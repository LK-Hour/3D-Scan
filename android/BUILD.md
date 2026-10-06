# Building and running the Android app

Requirements: Android Studio (Ladybug or newer), internet for the first Gradle sync (dependencies download once).

1. Open the `android/` folder in Android Studio ("Open", not "Import"). Accept the Gradle sync.
   If it asks for the Android SDK 35 platform, install it.
2. Tablet: Settings → About → tap *Build number* 7 times → Developer options → enable *USB debugging*. Connect by USB, accept the prompt.
3. Press **Run** (green triangle). The app installs as "Scan3D".
4. If sync or build fails: copy the **first** error into the chat. The debug APK and lint build on Linux with the
   pinned versions; camera behavior still needs a device check.

For a command-line build, run `./gradlew :app:assembleDebug :app:lintDebug` from `android/` with JDK 17 or newer
and Android SDK 35 installed. The APK is at `app/build/outputs/apk/debug/app-debug.apk`.

## First scan checklist
- Print 8+ AprilTags (family **tag36h11**, ids 0..7+), at 100 % scale, measure the black square (e.g. 16 cm). Stick them at different heights/walls.
- PC: `python -m scanpc serve` → QR in the browser. App: top right *Pair PC* → *Scan QR code*.
- *New scan* → name, tag size → *Start*. Walk slowly (the app says "Move slower"), overlap walls, keep tags in view often, loop back to the start.
- *Finish*, open the scan, add 2+ laser distances between tag centres (tag ids), *Upload*, *Process*, *Download*.

## Without Wi-Fi
- Copy the folder: `adb pull /sdcard/Android/data/com.scan3d.capture/files/sessions/<id> .` then `python -m scanpc process <folder>`.
- USB tunnel for upload: `adb reverse tcp:8765 tcp:8765`, then pair manually with host `127.0.0.1`.

## Tests that run without Android
`KOTLINC=/path/to/kotlinc android/core-tests/run.sh` (offline checks) and with `<baseUrl> <token> <sessionDir>` for the live upload test.
