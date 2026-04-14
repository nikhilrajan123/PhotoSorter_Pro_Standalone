[app]
title = PhotoSorter Pro
package.name = photosorter
package.domain = org.photosorter
source.dir = .
source.include_exts = py,kv,atlas
version = 1.1

# ── Python requirements ───────────────────────────────────────────────
# Keep minimal — pillow-heif and piexif can be installed at runtime
requirements = python3,kivy==2.3.0,pillow,requests

# ── Orientation ───────────────────────────────────────────────────────
orientation = portrait
fullscreen = 0

# ── Android API targets ───────────────────────────────────────────────
# api=34 (Android 14) removes Play Protect "old version" warning
android.api = 34
android.minapi = 26
android.ndk = 25b

# ── Architecture ──────────────────────────────────────────────────────
android.archs = arm64-v8a

# ── Permissions ───────────────────────────────────────────────────────
# Android 13+ (API 33+) replaced READ_EXTERNAL_STORAGE with granular media perms
# MANAGE_EXTERNAL_STORAGE = access ALL files (needed to read/write DCIM)
android.permissions = INTERNET,READ_MEDIA_IMAGES,READ_MEDIA_VIDEO,READ_MEDIA_AUDIO,READ_EXTERNAL_STORAGE,WRITE_EXTERNAL_STORAGE,MANAGE_EXTERNAL_STORAGE

# ── Android features ──────────────────────────────────────────────────
android.allow_backup = True
android.accept_sdk_license = True

# ── Gradle / build ────────────────────────────────────────────────────
android.gradle_repositories = "mavenCentral()"
android.gradle_dependencies = com.arthenica:ffmpeg-kit-video:6.0-2

[buildozer]
log_level = 2
warn_on_root = 1
