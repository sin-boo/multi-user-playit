# MULTI-USER for Blender (playit.gg edition)

> Enable real-time collaborative workflow inside Blender — simplified hosting with [playit.gg](https://playit.gg)

<img src="https://i.imgur.com/X0B7O1Q.gif" width=600>

:warning: Under development, use it at your own risks. Currently tested on Windows platform. :warning:

This is a modified version of [Multi-User](https://gitlab.com/slumber/multi-user) by Swann Martinez, optimized for hosting over the internet with playit.gg. No port forwarding or manual firewall setup required — create a TCP tunnel, host in Blender, and share the tunnel address for others to join.

Licensed under the same [GNU GPL v3](LICENSE) as the original project.

## playit.gg quick start

see <a href="https://multi-user-playit.mintlify.app/getting-started" target="_blank" rel="noopener noreferrer">setup guide</a> for the full walkthrough.

1. **Host:** Run the playit.gg agent and create a **TCP** tunnel with **port count 3**, local IP `127.0.0.1`, local port matching Blender's host port (default `5555`).
2. **Host:** In Blender, Multi-User panel → set Port → **Host**.
3. **Join:** Add a server preset and paste the full playit address (e.g. `my-tunnel.gl.at.ply.gg:41234`) into the IP field — the port is parsed automatically.

The addon uses three consecutive TCP ports: base (commands), base+1 (data), base+2 (TTL).

This tool allows multiple users to work on the same scene over the network. Based on a clients/server architecture, the data-oriented replication schema replicates Blender data-blocks across the wire.

## Installation

No official release yet. For now, install from this repo:

1. Clone or download the source from [GitHub](https://github.com/sin-boo/multi-user-playit).
2. Zip the `multi_user` folder (or use a pre-built `multi_user-0.7.4-pg.zip` if available in [Releases](https://github.com/sin-boo/multi-user-playit/releases)).
3. In Blender: Edit → Preferences → Add-ons → Install from disk → select the zip.

[Dependencies](#dependencies) are added to your Blender Python automatically during installation.

## Usage

See the [original Multi-User documentation](https://slumber.gitlab.io/multi-user/index.html) for general addon usage. For internet hosting, follow the playit.gg quick start above instead of the upstream port-forwarding guides.

## Dependencies

| Dependencies | Version | Needed |
| ------------ | :-----: | -----: |
| Replication  | latest  |    yes |

## Community

For general Multi-User help and discussion with the original creators, feel free to [join their Discord server](https://discord.gg/aBPvGws).

## Licensing

This project is based on [Multi-User](https://gitlab.com/slumber/multi-user) and remains under the [GNU General Public License v3](LICENSE).

See [license](LICENSE)
