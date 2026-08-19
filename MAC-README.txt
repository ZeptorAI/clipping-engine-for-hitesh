CLIP EDITOR — Mac setup
=======================

STEP 1 — make the launchers runnable (one time only)
  1. Open Terminal  (press Cmd+Space, type "Terminal", hit Enter)
  2. Type this, INCLUDING the space at the end:      chmod +x
  3. Drag the files "setup.command" and "start.command" into the Terminal window
     (they'll turn into file paths)
  4. Press Enter.  Nothing visible happens — that's fine.

STEP 2 — install (one time only)
  - Double-click  setup.command
  - If macOS warns "unidentified developer": right-click it -> Open -> Open
  - It installs everything (Homebrew, ffmpeg, Python bits). Takes a few minutes.
  - When it says "Setup complete", close the window.

STEP 3 — run it (every time)
  - Double-click  start.command
  - A black window opens (keep it open) and your browser opens the app.
  - Drop a video, hit "Make clips", get your clips.
  - Close the black window when you're done.

That's it. If anything errors, screenshot the message and send it back.
