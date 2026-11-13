# Chameleon

A small chameleon that sits on top of a window or a widget and changes colour to match it.

He pops up on the active window. Drag him onto any window or widget and he perches on the nearest edge, facing in. Scroll, or middle-drag, to turn him. Right-click him to take him off the desktop.

The colour is measured from the window he is on. That sample stays on this computer and is deleted after the colour is read. Nothing is uploaded.

## Install

Needs GNOME, Python 3, GTK 3, PyGObject, Pillow, cairo, and AT-SPI (`python3-gi`, `python3-gi-cairo`, `gir1.2-gtk-3.0`, `gir1.2-atspi-2.0`, `python3-pil`). The desktop has to be using XWayland so he can sit on a window.

```bash
git clone https://github.com/burnt1983/chameleon.git
cd chameleon
bash install.sh
chameleon
```

Pictures unpack into `~/.local/share/chameleon/`. It does not start at login.

## How to use it

| Action | What happens |
|---|---|
| Drag him | He follows the pointer, then perches on the nearest edge of the window or widget underneath |
| Drop him on the empty desktop | He stays where you left him |
| Scroll, or middle-drag | Turn him |
| Right-click him | Quit |

## Uninstall

```bash
bash uninstall.sh
```

## Licence

MIT. The chameleon pictures are part of this project. See [LICENSE](LICENSE).
