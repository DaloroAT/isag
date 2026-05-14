# Isag

**Isag** (/aɪˈzɑɡ/, "Isolated Agent") — sandboxes coding agents in
containers with hard limits on what it can
read, write, and reach.

## Why

Isag gives the agent full freedom inside a container, while keeping
your machine safe outside it. You get:

- **A filesystem scoped to what you mount.** The agent sees only the
  directories you explicitly add. Your home directory, your SSH keys,
  the rest of your host — invisible.
- **Read-only mounts when you want them.** Mount your project `:ro` to
  let the agent analyze without editing. Mount datasets `:ro`. Mount a
  scratch directory `:rw`. The kernel rejects writes to a read-only
  bind mount regardless of file permissions.
- **A network firewall.** Outbound traffic is locked to a domain
  allowlist enforced at the kernel level. Anything else fails to
  connect — the agent can't lift the rule from inside.
- **Optional GPU passthrough.** Flip one flag in the config to give the
  container CUDA + the NVIDIA toolkit.
- **Disposable container.** Try experimental tooling without
  consequence — pip at runtime, system packages via
  `extra_packages` + rebuild. If something breaks, the host is
  untouched and Isag brings up a clean one.

## Requirements

- Linux host with Docker.
- GPU mode needs the NVIDIA Container Toolkit.
- Tested on Linux and WSL2. macOS (Docker Desktop / Rancher Desktop) and
  native Windows Docker should work but are untested.

## Try it
Install:
```bash
pip install isag
```

Run:
```bash
isag init
isag run
```

You're inside the agent CLI now, in a container that can reach github.com,
pypi.org, npm, and the vendor's API — and nothing else.

Expose a container service on the host's loopback. E.g., reach the container's SSH or Jupyter from your host (useful for IDEs and browsers):
```bash
isag ssh -L 2222:localhost:22 -L 8888:localhost:8888
```

Expose a host service to the container. E.g., give the container access to the host's `adb` server, and through it any 
devices the host can reach (USB, network, or via VPN):
```bash
isag ssh -R 5037:localhost:5037
```

## The config file

`isag init` writes a starter `isag.yaml`. The lines you'll touch:

```yaml
project: ~/code/my-project:/workspace/project:rw   # use :ro for analysis-only runs
agent:
  vendor: claude                                   # claude or codex
  host_home: ~/agents                              # vendors persist here as host_home/.claude, host_home/.codex; set to ~ to share history + credentials with your host install
container:
  python: 3.14                                     # container system-wide Python
  image: ubuntu24.04                               # or e.g. nvidia/cuda:12.8.1-runtime-ubuntu24.04 if gpu:true
  gpu: false                                       # true for CUDA + NVIDIA toolkit
  host_cache_dir: ~/isag-cache                     # mounting pip, npm, and other caches          
limit_network:
  domains:                                         # everything else is blocked
    - github.com
    - pypi.org
    - registry.npmjs.org
mounts:
  - ~/datasets:/workspace/datasets:ro              # add more mounts; :ro makes them read-only
exclude:                                           # hide paths inside any mount (set to null to disable)
  files:
    - .env
  folders:
    - .git
```

- Set `limit_network: null` to turn the firewall off entirely. Useful on
trusted networks; not the default for a reason.
- Excluded paths are host paths; if they fall under `project` or any `mounts`
entry, the corresponding container path is overlaid with an empty mount. 
- Both absolute and relative paths on the host are permitted for all fields. If a field path is 
relative, it is resolved relative to the project host path. If the project host path is also relative, it is first 
resolved relative to the YAML file path.
- Command `isag run` displays the cache directory. Open it to view the files and commands used to run the project.

## What it doesn't protect

- Anything you mount writable — the agent has full access there.
- Sibling containers, if you opt into `external_networks`.
- Anything the agent can do at an allowlisted endpoint with credentials you gave it.

## License

Apache-2.0.
