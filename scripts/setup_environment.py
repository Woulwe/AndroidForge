#!/usr/bin/env python3
"""AndroidForge — Toolchain Setup.

Reads the JSON produced by detect_project.py and config/toolchain-rules.yaml,
then decides:

  • Which JDK version to install
  • Which Gradle version to install (if no usable wrapper)
  • Which AGP version is in use
  • Which NDK version to install (if any)
  • Whether Flutter is needed
  • Which compatibility fixes (legacy_fixes) to apply

The script writes its decisions to $GITHUB_OUTPUT and prints a JSON summary.
It also applies non-destructive compatibility fixes to the build workspace
(NEVER to the original source ZIP).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

try:
    import yaml  # PyYAML
except ImportError:  # pragma: no cover
    print("ERROR: PyYAML not installed. Run: pip install pyyaml", file=sys.stderr)
    sys.exit(2)


REPO_ROOT = Path(__file__).resolve().parent.parent
RULES_PATH = REPO_ROOT / "config" / "toolchain-rules.yaml"


def load_rules(path: Path | None = None) -> dict[str, Any]:
    p = path or RULES_PATH
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    return data or {}


def major(v: str) -> int:
    try:
        return int(str(v).split(".")[0])
    except (ValueError, IndexError):
        return 0


def minor(v: str) -> int:
    try:
        return int(str(v).split(".")[1])
    except (ValueError, IndexError):
        return 0


def pick_jdk_for_agp(agp: str | None, rules: dict) -> str:
    """Determine JDK version required by AGP."""
    if not agp:
        return "17"  # safe modern default
    m = major(agp)
    if m >= 8:
        return "17"
    if m == 7:
        return "11"
    if m == 4:
        return "11"
    if m <= 3:
        return "8"
    # Fallback to version-catalog rules
    for pattern, jdk in rules.get("agp_to_jdk", {}).items():
        if pattern.endswith(".x"):
            pm = pattern[:-2]
            if str(agp).startswith(pm):
                return str(jdk)
        elif str(agp).startswith(pattern):
            return str(jdk)
    return "17"


def pick_jdk_for_gradle(gradle: str | None, rules: dict) -> str:
    if not gradle:
        return "17"
    m = major(gradle)
    if m >= 8:
        return "17"
    if m == 7:
        return "11"
    if m <= 6:
        return "8"
    return "17"


def pick_gradle_for_agp(agp: str | None, rules: dict) -> str | None:
    """Look up the minimum Gradle version required by a given AGP version."""
    if not agp:
        return None
    mapping = rules.get("agp_to_gradle", {})
    # Try exact match first
    if agp in mapping:
        return mapping[agp]
    # Try prefix match (e.g. "7.4.2" → "7.4")
    parts = str(agp).split(".")
    for i in range(len(parts), 0, -1):
        prefix = ".".join(parts[:i])
        if prefix in mapping:
            return mapping[prefix]
    # Fall back to major-version scan
    m = major(agp)
    for k, v in mapping.items():
        if major(k) == m:
            return v
    return None


def pick_flutter_version(detect: dict, rules: dict) -> str:
    flutter = detect.get("flutter", {}) or {}
    constraint = flutter.get("flutter_version_constraint")
    if constraint:
        m = re.search(r"(\d+\.\d+\.\d+)", constraint)
        if m:
            return m.group(1)
    return str(rules.get("flutter", {}).get("default_version", "3.24.0"))


def pick_ndk_version(detect: dict, rules: dict) -> str:
    ndk_v = (detect.get("versions") or {}).get("ndk_version")
    if ndk_v:
        return str(ndk_v)
    return str(rules.get("ndk", {}).get("default_version", "26.1.10909125"))


def determine_legacy_fixes(detect: dict, rules: dict) -> list[dict]:
    fixes: list[dict] = []
    wrapper = detect.get("wrapper", {}) or {}
    gradle_v = wrapper.get("version")
    has_local = "local.properties" in detect.get("indicator_files", {})

    if wrapper.get("missing_jar") or not wrapper.get("present"):
        fixes.append({"name": "patch_gradle_wrapper", "reason": "Missing or corrupt gradle-wrapper.jar"})

    if wrapper.get("uses_http"):
        fixes.append({"name": "migrate_https", "reason": "Wrapper uses http:// distribution URL"})

    if gradle_v and major(gradle_v) < 4:
        fixes.append({"name": "disable_gradle_daemon", "reason": f"Very old Gradle: {gradle_v}"})

    if not has_local and detect.get("project_type") in ("gradle", "flutter"):
        fixes.append({"name": "inject_local_properties", "reason": "local.properties missing"})

    return fixes


def apply_fixes(detect: dict, fixes: list[dict], android_sdk_root: str) -> list[str]:
    """Apply non-destructive compatibility fixes to the project tree."""
    applied: list[str] = []
    root = Path(detect["project_root"])
    props = root / "gradle" / "wrapper" / "gradle-wrapper.properties"

    for fix in fixes:
        name = fix["name"]
        if name == "migrate_https" and props.exists():
            content = props.read_text(encoding="utf-8", errors="replace")
            new_content = re.sub(r"distributionUrl=http://", "distributionUrl=https://", content)
            if new_content != content:
                props.write_text(new_content)
                applied.append(f"Migrated wrapper distribution URL to HTTPS in {props}")
        elif name == "inject_local_properties":
            (root / "local.properties").write_text(f"sdk.dir={android_sdk_root}\n")
            applied.append(f"Created local.properties with sdk.dir={android_sdk_root}")
        elif name == "disable_gradle_daemon":
            gradle_props = root / "gradle.properties"
            content = gradle_props.read_text() if gradle_props.exists() else ""
            if "org.gradle.daemon" not in content:
                with gradle_props.open("a") as f:
                    f.write("\n# AndroidForge: disable Gradle daemon for old Gradle\norg.gradle.daemon=false\n")
                applied.append("Disabled Gradle daemon via gradle.properties")
        elif name == "patch_gradle_wrapper":
            # The workflow will run `gradle wrapper` to regenerate; we just flag it.
            applied.append("Flagged wrapper for regeneration via `gradle wrapper`")

    # ---- Always ensure gradle.properties has a reasonable JVM heap ----
    # The GitHub-hosted ubuntu-latest runner has ~7 GB RAM. The Gradle
    # daemon's default heap is often too small for Flutter / multi-module
    # projects, leading to "Gradle build daemon has been stopped: since
    # the JVM garbage collector is thrashing" during flutter build apk.
    # We inject `org.gradle.jvmargs=-Xmx4g` in THREE places:
    #   1. <project_root>/gradle.properties  (root-level, for plain Gradle projects)
    #   2. <project_root>/android/gradle.properties  (Flutter projects invoke Gradle from android/)
    #   3. ~/.gradle/gradle.properties  (global, applies to ALL Gradle invocations)
    HEAP_LINE = "org.gradle.jvmargs=-Xmx4g -XX:+UseG1GC -Dfile.encoding=UTF-8"
    # AndroidX is required by most modern Flutter plugins. If a project
    # doesn't enable it explicitly, Gradle aborts with:
    #   "Configuration :app:debugRuntimeClasspath contains AndroidX
    #    dependencies, but the android.useAndroidX property is not enabled."
    # We proactively set useAndroidX=true + enableJetifier=true so legacy
    # support-library references are auto-migrated. Both flags are no-ops
    # for projects that already use AndroidX, so this is safe to set always.
    ANDROIDX_LINES = [
        "android.useAndroidX=true",
        "android.enableJetifier=true",
    ]

    def _inject_gradle_properties(props_path: Path, location_label: str) -> None:
        if props_path.exists():
            content = props_path.read_text(encoding="utf-8", errors="replace")
            changes: list[str] = []
            # Handle jvmargs
            m = re.search(r"^org\.gradle\.jvmargs\s*=\s*(.+)$", content, re.MULTILINE)
            if m:
                existing = m.group(1).strip()
                heap_match = re.search(r"-Xmx(\d+)([gm])", existing, re.IGNORECASE)
                if heap_match:
                    size = int(heap_match.group(1))
                    unit = heap_match.group(2).lower()
                    existing_mb = size * (1024 if unit == "g" else 1)
                    if existing_mb < 4096:
                        new_content = re.sub(
                            r"^org\.gradle\.jvmargs\s*=.*$",
                            HEAP_LINE,
                            content,
                            flags=re.MULTILINE,
                        )
                        if new_content != content:
                            content = new_content
                            changes.append(f"bumped jvmargs from '{existing}'")
                # else: heap is already >= 4g, leave alone
            else:
                content += f"\n# AndroidForge: ensure enough heap for the Gradle daemon\n{HEAP_LINE}\n"
                changes.append("added jvmargs")
            # Handle android.useAndroidX
            if not re.search(r"^android\.useAndroidX\s*=", content, re.MULTILINE):
                content += f"\n# AndroidForge: enable AndroidX (most Flutter plugins need it)\n"
                for line in ANDROIDX_LINES:
                    content += f"{line}\n"
                changes.append("added useAndroidX=true + enableJetifier=true")
            if changes:
                props_path.write_text(content)
                applied.append(f"Updated {location_label}: {', '.join(changes)}")
        else:
            props_path.parent.mkdir(parents=True, exist_ok=True)
            content = (
                f"# AndroidForge: ensure enough heap for the Gradle daemon\n"
                f"{HEAP_LINE}\n"
                f"org.gradle.daemon=false\n"
                f"\n# AndroidForge: enable AndroidX (most Flutter plugins need it)\n"
            )
            for line in ANDROIDX_LINES:
                content += f"{line}\n"
            props_path.write_text(content)
            applied.append(f"Created {location_label} with jvmargs + useAndroidX + enableJetifier")

    # 1. Project root gradle.properties
    _inject_gradle_properties(root / "gradle.properties", "gradle.properties (root)")
    # 2. For Flutter projects: android/gradle.properties (this is the one
    #    Gradle actually reads when invoked from the android/ subdir).
    android_dir = root / "android"
    if android_dir.is_dir():
        _inject_gradle_properties(android_dir / "gradle.properties", "android/gradle.properties (Flutter)")
    # 3. Global gradle.properties in the user's home — applies to ALL
    #    Gradle invocations on the runner regardless of project.
    home_gradle = Path.home() / ".gradle" / "gradle.properties"
    _inject_gradle_properties(home_gradle, "~/.gradle/gradle.properties (global)")

    # ---- Inject common public Maven repos (JitPack, Google, mavenCentral) ----
    # Many projects (especially Flutter plugins) depend on libraries hosted
    # on JitPack (group ID starts with 'com.github.'). If the project's
    # build.gradle / settings.gradle doesn't declare the JitPack repo, the
    # build fails with "Could not find com.github.foo:bar:x.y.z".
    # We proactively inject these repos into the project's gradle files.
    _inject_repositories(root, applied, is_flutter=bool((detect.get("flutter") or {}).get("is_flutter")))

    # Always disable build cache & parallel for very old gradle (safer)
    return applied


def _inject_repositories(root: Path, applied: list[str], is_flutter: bool) -> None:
    """Inject JitPack / Google / mavenCentral into the project's gradle files
    if not already declared. Non-destructive: only adds, never removes.
    """
    # Candidates: for Flutter, the relevant files are under android/;
    # for plain Gradle, they're at the root.
    candidates: list[Path] = []
    if is_flutter:
        candidates = [
            root / "android" / "build.gradle",
            root / "android" / "build.gradle.kts",
            root / "android" / "settings.gradle",
            root / "android" / "settings.gradle.kts",
        ]
    candidates += [
        root / "build.gradle",
        root / "build.gradle.kts",
        root / "settings.gradle",
        root / "settings.gradle.kts",
    ]

    REPO_LINES_KTS = [
        'maven { url = uri("https://maven.google.com") }',
        'mavenCentral()',
        'maven { url = uri("https://plugins.gradle.org/m2") }',
        'maven { url = uri("https://jitpack.io") }',
    ]
    REPO_LINES_GROOVY = [
        'maven { url \'https://maven.google.com\' }',
        'mavenCentral()',
        'maven { url \'https://plugins.gradle.org/m2\' }',
        'maven { url \'https://jitpack.io\' }',
    ]

    # If settings.gradle(.kts) owns dependency repositories, Gradle rejects
                r"repositoriesMode\s*\.\s*set\s*\(\s*RepositoriesMode\s*\.\s*(?:PREFER_SETTINGS|FAIL_ON_PROJECT_REPOS)",
    # PREFER_SETTINGS or FAIL_ON_PROJECT_REPOS. Detect this before processing
    # candidates so we only inject into settings-level repositories.
    settings_files = [p for p in candidates if p.name.startswith("settings.gradle")]
    settings_owns_repositories = False
    for settings_file in settings_files:
        if settings_file.exists():
            settings_text = settings_file.read_text(encoding="utf-8", errors="replace")
            if re.search(
                r"repositoriesMode\\s*\\.\\s*set\\s*\\(\\s*RepositoriesMode\\s*\\.\\s*(?:PREFER_SETTINGS|FAIL_ON_PROJECT_REPOS)",
                settings_text,
                re.IGNORECASE,
            ):
                settings_owns_repositories = True
                break

    for grad in candidates:
        if not grad.exists():
            continue
        if settings_owns_repositories and grad.name.startswith("build.gradle"):
            applied.append(
                f"Skipped repository injection into {grad.relative_to(root) if grad.is_relative_to(root) else grad}: settings.gradle owns repositories"
            )
            continue
        content = grad.read_text(encoding="utf-8", errors="replace")
        # Skip if all repos are already declared anywhere in the file.
        already_has = (
            "jitpack.io" in content
            and "maven.google.com" in content
            and "mavenCentral()" in content
        )
        if already_has:
            continue
        # Try to find a `repositories { ... }` block and append our repos
        # just before its closing brace.
        is_kts = grad.name.endswith(".kts")
        repo_lines = REPO_LINES_KTS if is_kts else REPO_LINES_GROOVY
        # Strategy 1: find a top-level `repositories {` block and inject.
        new_content = content
        injected = False
        # Find every `repositories {` occurrence (case-insensitive).
        for m in reversed(list(re.finditer(r"repositories\s*\{", content, re.IGNORECASE))):
            # Find the matching closing brace by counting.
            start = m.end()
            depth = 1
            i = start
            while i < len(content) and depth > 0:
                ch = content[i]
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                i += 1
            if depth == 0:
                # i-1 is the index of the closing brace.
                close_idx = i - 1
                # Check which repos are missing from this block.
                block_content = content[start:close_idx]
                missing = []
                for line, marker in zip(repo_lines, ["maven.google.com", "mavenCentral()", "plugins.gradle.org", "jitpack.io"]):
                    if marker not in block_content:
                        missing.append(line)
                if not missing:
                    continue
                inject_text = "\n        // AndroidForge: ensure common public Maven repos\n        " + "\n        ".join(missing) + "\n"
                # Insert just before the closing brace.
                new_content = new_content[:close_idx] + inject_text + new_content[close_idx:]
                injected = True
        if not injected:
            # Strategy 2: append a top-level allprojects block.
            # (works for old Gradle versions, ignored by new ones with
            # FAIL_ON_PROJECT_REPOS but we tried strategy 1 first.)
            block = "\n\n// AndroidForge: ensure common public Maven repos\nallprojects {\n    repositories {\n"
            for line in repo_lines:
                block += "        " + line + "\n"
            block += "    }\n}\n"
            new_content = content + block
            injected = True
        if injected and new_content != content:
            grad.write_text(new_content)
            applied.append(f"Injected JitPack/Google/mavenCentral into {grad.relative_to(root) if grad.is_relative_to(root) else grad}")


def main() -> int:
    parser = argparse.ArgumentParser(description="AndroidForge toolchain setup")
    parser.add_argument("--root", required=True, help="Project root path")
    parser.add_argument("--detect", required=True, help="Detection JSON (string or file path)")
    parser.add_argument("--rules", default=None, help="Path to toolchain-rules.yaml (default: <repo>/config/...)")
    parser.add_argument("--android-sdk", default=os.environ.get("ANDROID_SDK_ROOT", "/usr/local/lib/android/sdk"))
    parser.add_argument("--output", default=None, help="Write JSON summary to this file")
    args = parser.parse_args()

    rules = load_rules(Path(args.rules) if args.rules else None)

    # Parse detection JSON
    if Path(args.detect).exists():
        detect = json.loads(Path(args.detect).read_text())
    else:
        detect = json.loads(args.detect)

    versions = detect.get("versions", {}) or {}
    wrapper = detect.get("wrapper", {}) or {}
    agp = versions.get("agp_version")
    gradle_wrapper_v = wrapper.get("version")
    java_in_build = versions.get("java_version")
    kotlin_v = versions.get("kotlin_version")

    # Pick JDK
    jdk_from_agp = pick_jdk_for_agp(agp, rules)
    jdk_from_gradle = pick_jdk_for_gradle(gradle_wrapper_v, rules)
    # Use the highest of (agp req, gradle req, project's own target)
    jdk_candidates = [jdk_from_agp, jdk_from_gradle]
    if java_in_build:
        jdk_candidates.append(str(java_in_build))
    chosen_jdk = max(jdk_candidates, key=lambda v: (major(v), minor(v)))

    # Pick Gradle
    chosen_gradle = gradle_wrapper_v  # prefer the bundled wrapper version
    if not chosen_gradle:
        chosen_gradle = pick_gradle_for_agp(agp, rules)
    if not chosen_gradle:
        chosen_gradle = "8.0"  # safe modern fallback

    # Determine if wrapper can be used
    use_wrapper = bool(wrapper.get("present"))
    if wrapper.get("uses_http"):
        # We can fix it; still usable
        use_wrapper = True

    # Pick Flutter
    needs_flutter = bool((detect.get("flutter") or {}).get("is_flutter"))
    chosen_flutter = pick_flutter_version(detect, rules) if needs_flutter else None

    # Pick NDK
    needs_ndk = bool((detect.get("native") or {}).get("has_native"))
    chosen_ndk = pick_ndk_version(detect, rules) if needs_ndk else None

    # Determine legacy fixes
    fixes = determine_legacy_fixes(detect, rules)
    applied_fixes = apply_fixes(detect, fixes, args.android_sdk)

    # Decide build commands. Prep commands run unconditionally first (e.g.
    # `flutter pub get` to download dependencies); build commands are tried
    # in order and the first one to succeed marks the build as successful.
    # Prep commands must NOT be in build_commands — otherwise build_project.py
    # would stop on `flutter pub get` (which exits 0 without producing an APK)
    # and never actually run `flutter build apk`.
    prep_commands: list[list[str]] = []
    if needs_flutter:
        prep_commands = [["flutter", "pub", "get"]]
        build_commands = [
            ["flutter", "build", "apk", "--debug"],
            ["flutter", "build", "apk", "--release"],
        ]
    else:
        # Use gradlew if present, otherwise use installed gradle
        gradlew = Path(detect["project_root"]) / "gradlew"
        gradle_cmd = str(gradlew) if use_wrapper and gradlew.exists() else "gradle"
        build_commands = [
            [gradle_cmd, "assembleDebug"],
            [gradle_cmd, "assembleRelease"],
            [gradle_cmd, "bundleDebug"],
        ]

    result = {
        "project_root": detect["project_root"],
        "jdk_version": chosen_jdk,
        "gradle_version": chosen_gradle,
        "agp_version": agp,
        "kotlin_version": kotlin_v,
        "ndk_version": chosen_ndk,
        "flutter_version": chosen_flutter,
        "use_gradle_wrapper": use_wrapper,
        "needs_flutter": needs_flutter,
        "needs_ndk": needs_ndk,
        "needs_gradle": detect.get("project_type") in ("gradle", "flutter"),
        "legacy_fixes_applied": applied_fixes,
        "prep_commands": prep_commands,
        "build_commands": build_commands,
    }

    # Output JSON
    output = json.dumps(result, indent=2)
    if args.output:
        Path(args.output).write_text(output)
    else:
        print(output)

    # Write to GITHUB_OUTPUT
    gh_output = os.environ.get("GITHUB_OUTPUT")
    if gh_output:
        with open(gh_output, "a", encoding="utf-8") as f:
            f.write(f"jdk_version={chosen_jdk}\n")
            f.write(f"gradle_version={chosen_gradle}\n")
            if agp:
                f.write(f"agp_version={agp}\n")
            if kotlin_v:
                f.write(f"kotlin_version={kotlin_v}\n")
            if chosen_ndk:
                f.write(f"ndk_version={chosen_ndk}\n")
            f.write(f"needs_flutter={'true' if needs_flutter else 'false'}\n")
            f.write(f"needs_ndk={'true' if needs_ndk else 'false'}\n")
            f.write(f"needs_gradle={'true' if result['needs_gradle'] else 'false'}\n")
            f.write(f"use_wrapper={'true' if use_wrapper else 'false'}\n")
            if chosen_flutter:
                f.write(f"flutter_version={chosen_flutter}\n")
            f.write(f"json<<EOF\n{json.dumps(result)}\nEOF\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
