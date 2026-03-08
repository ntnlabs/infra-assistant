# Slurm Wrapper (`bob-slurm`)

This folder contains a restricted wrapper for Slurm node operations.

## Why this wrapper exists

- Bob should not run raw shell commands like `scontrol` directly.
- `bob-slurm` exposes a minimal audited command surface:
  - `summary` - Cluster node summary
  - `node-status` - Individual node status
  - `drain` - Drain a node (requires reason)
  - `resume` - Resume a drained node
  - `queue` - Job queue (current running/pending jobs)
  - `job` - Detailed job information
  - `history` - Historical job data (completed/failed jobs via sacct)

## Install on Slurm Master

1. Copy script:

```bash
sudo install -o root -g root -m 0750 bob-slurm /usr/local/bin/bob-slurm
```

2. Add sudoers policy (adjust username):

```bash
sudo cp bob-slurm.sudoers /etc/sudoers.d/bob-slurm
sudo visudo -cf /etc/sudoers.d/bob-slurm
```

3. Verify manually:

```bash
/usr/local/bin/bob-slurm summary
/usr/local/bin/bob-slurm node-status --node <node>
/usr/local/bin/bob-slurm queue
/usr/local/bin/bob-slurm queue --user username
/usr/local/bin/bob-slurm job --jobid 12345
/usr/local/bin/bob-slurm history --state FAILED --hours 24
sudo /usr/local/bin/bob-slurm drain --node <node> --reason "ops ticket-123 maintenance"
sudo /usr/local/bin/bob-slurm resume --node <node>
```

## Infra Assistant Integration

1. Add Slurm master host alias to `ssh-proxy/hosts.yaml`:

```yaml
hosts:
  - name: "slurm-master"
    hostname: "10.0.0.10"
    username: "monitor"
    key_file: "/path/to/key"
```

2. Set env vars in `.env`:

```bash
SLURM_MASTER_HOST=slurm-master
SLURM_WRAPPER_COMMAND=/usr/local/bin/bob-slurm
SLURM_DEFAULT_PARTITION=
```

If Bob runs as a non-root user, set `SLURM_WRAPPER_COMMAND=sudo -n /usr/local/bin/bob-slurm` and configure sudoers accordingly.

3. Ensure `ssh-proxy/commands.yaml` includes the `bob-slurm` allowlist patterns.

## Notes

- `drain` and `resume` are mutating and should require explicit confirmation in Bob.
- Keep wrapper behavior deterministic and JSON-only for reliable parsing.
