CLIP EDITOR — Mac setup
=======================

STEP 1 — unlock the files (one time only)
  macOS blocks apps downloaded from the internet ("cannot verify it's free of
  malware"). This is normal. To unblock:

  1. Open Terminal  (press Cmd+Space, type "Terminal", hit Enter)

  2. Type this, INCLUDING the space at the end:      xattr -cr
     Then drag the whole "ClipEditor-Mac" FOLDER into the Terminal window
     (it becomes a path). Press Enter.

  3. Type this, INCLUDING the space at the end:      chmod +x
     Then drag the files "setup.command" and "start.command" into the window.
     Press Enter.

  (Nothing visible happens — that's fine. The files are now unlocked.)

STEP 2 — install (one time only)
  - Double-click  setup.command
  - It installs everything (Homebrew, ffmpeg, git, Python bits). A few minutes.
  - When it says "Setup complete", close the window.

STEP 3 — run it (every time)
  - Double-click  start.command
  - A black window opens (keep it open) and your browser opens the app.
  - Drop a video, hit "Make clips", get your clips.
  - Close the black window when you're done.
  (It quietly checks for updates each time you launch — no action needed.)

That's it. If anything errors, screenshot the message and send it back.
