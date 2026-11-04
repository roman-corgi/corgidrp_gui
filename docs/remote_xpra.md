# Running corgidrp GUI on a server, displayed on your Mac

The GUI runs where corgidrp and the data are. xpra shows its windows on your laptop.

xpra starts a private virtual X server on the remote machine. The GUI draws into it at full
speed, and xpra streams compressed per-window updates to a client on your Mac, where each
window appears as a native window. Unlike plain X forwarding, drawing is not synchronous over
the link, Qt's bitmap-heavy repaints are compressed, and the session survives a dropped
connection: you can detach, close the laptop, and reattach later.

## 1. Server (Linux): install xpra

Ubuntu 22.04 or newer, Debian 12 or newer, Fedora, Rocky/Alma:

    curl https://xpra.org/get-xpra.sh | bash
    sudo apt install xpra          # or dnf

**Ubuntu 20.04 (focal)**: the script above fails with a 404, because the current xpra source
tree no longer carries a repository file for focal. xpra.org still serves a signed focal
repository (xpra 5.0.x). Write the repository file by hand:

    sudo curl -o /usr/share/keyrings/xpra.asc https://xpra.org/xpra.asc
    sudo tee /etc/apt/sources.list.d/xpra.sources <<'EOF'
    Types: deb
    URIs: https://xpra.org
    Suites: focal
    Components: main
    Signed-By: /usr/share/keyrings/xpra.asc
    EOF
    sudo apt update && sudo apt install xpra

Check: `xpra --version`.

## 2. Server: install the GUI and Qt's X11 libraries

In the conda environment where corgidrp imports:

    conda activate corgidrp
    pip install -e /path/to/corgidrp_gui

Qt 6 needs a handful of X11 client libraries that minimal servers lack (Debian/Ubuntu names;
`libxcb-cursor0` is the one most often missing):

    sudo apt install libxcb-cursor0 libxcb-xinerama0 libxkbcommon-x11-0 \
                     libxcb-icccm4 libxcb-keysyms1 libxcb-shape0 \
                     libgl1 libegl1 libfontconfig1 libdbus-1-3

Then find the absolute path of the GUI's entry point and keep it; xpra starts the program by
that path, so no shell setup or `conda activate` is needed on the xpra side:

    which corgidrp-gui
    # e.g. /home/maxmb/anaconda3/envs/corgidrp/bin/corgidrp-gui

Check that it runs headlessly:

    QT_QPA_PLATFORM=offscreen corgidrp-gui --version

## 3. Client (macOS): install xpra

Download **Xpra-Light** (or the full Xpra) DMG from https://xpra.org/dists/MacOS/ and drag
it to Applications. Xpra-Light omits video codecs, which the GUI does not need. The app does
not add itself to your shell path, so make an alias once:

    echo 'alias xpra="/Applications/Xpra-Light.app/Contents/MacOS/Xpra-Light"' >> ~/.bash_profile

(For the full build the path is `/Applications/Xpra.app/Contents/MacOS/Xpra`.)

A 6.x Mac client talks to a 5.0.x server on focal without trouble; versions do not have to
match exactly.

## 4. Use

Start a session on the server and attach to it, in one command from the Mac:

    xpra start ssh://user@server/ \
        --start-child=/home/user/anaconda3/envs/corgidrp/bin/corgidrp-gui \
        --exit-with-children

The Launcher window appears on your Mac. The session ends when you close the GUI.

To keep a session running across connections, leave out `--exit-with-children`; then:

    xpra attach ssh://user@server/      # from any machine, later
    xpra detach                         # or just close the client

On the server, `xpra list` shows sessions and `xpra stop` ends one.

Useful client flags: `--desktop-scaling=2` if windows come out tiny on a Retina display,
`--keyboard-layout=us` if keys are mismatched, `--encoding=rgb` on a fast campus link for the
sharpest text, `--opengl=no` if windows show garbage.

## Troubleshooting

Symptoms as they appear in the client's terminal output, and what they mean.

**`curl: (22) ... 404` / `failed to download .../repos/focal/xpra.sources`** while installing
on the server. Your server runs Ubuntu 20.04. Use the focal recipe in step 1.

**`exit-with-children was specified but start-child* is missing!`** The program was given
with `--start=` instead of `--start-child=`. Only start-child programs count as the session's
children.

**`activate: 1: Syntax error: Unterminated quoted string`** or similar shell errors right
after connecting. The `--start-child` value went through the remote shell and lost its quotes.
Do not try to wrap the command in `bash -lc '...'`; give the absolute path of
`corgidrp-gui` as in step 4. If you truly need environment variables, put them in a small
script on the server and start that:

    #!/bin/bash
    export CORGIDRP_GUI_HOME=/data/cgi/gui
    exec /home/user/anaconda3/envs/corgidrp/bin/corgidrp-gui "$@"

**Server starts, then "server requested disconnect: server shutdown" a few seconds later,
no window.** The GUI exited immediately. Find out why by running the server in the
foreground on the server, where the GUI's own error output is shown:

    ssh -t user@server 'xpra start :78 --daemon=no --exit-with-children \
        --start-child=/home/user/anaconda3/envs/corgidrp/bin/corgidrp-gui'

Look after the line `started command ...`. The usual causes:

- `Could not load the Qt platform plugin "xcb"` or `xcb-cursor0 or libxcb-cursor0 is
  needed`: install the libraries in step 2.
- `corgidrp-gui: No such file or directory`: wrong path; re-run `which corgidrp-gui` in the
  activated environment.
- A Python traceback ending in corgidrp: the package itself fails to import on that machine
  (an old scipy, a stale `DetectorParams` file). Fix it as you would for the plain walker.

**`You already have an xpra server running at ...`** when starting a numbered display. A
previous attempt left a session behind:

    xpra stop :78; xpra clean-sockets

**`failed to receive anything, not an xpra server?` / `SSH connection to the xpra server
failed`** right at the start. Either xpra is not installed on the server yet (`ssh server
xpra --version`), or the address is malformed: use a single trailing slash,
`ssh://user@server/`, not `//`.

**`cannot create group socket '/run/xpra/...' Permission denied`** and warnings about
webcams, printers, `vaapih264enc`, OpenGL or `uinput` in the server log are harmless; the
GUI uses none of those.

**Windows come out tiny or huge.** Add `--desktop-scaling=2` (or `1`) to the client command.

## Plain X11 forwarding

    ssh -Y user@server
    conda activate corgidrp && corgidrp-gui

Works with XQuartz on the Mac and needs no extra software on the server beyond Qt's xcb
libraries, but every repaint crosses the network and the session dies with the SSH connection.
Fine for a quick look on a fast link; use xpra for real work.

## Why there is no "remote backend" in the app

Because the whole GUI runs on the server, every file it sees is local: the file dialogs, the
CalDB, the queue and the runner processes all work unchanged. A split GUI (local windows,
remote agent) would need its own file browser and a protocol; the code keeps one small
discipline from that design, a `Backend` interface for file and process access, so it remains
possible later without a rewrite.
