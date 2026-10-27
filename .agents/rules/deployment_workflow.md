# Cluster Code Deployment & Synchronization Rule

All code changes across PersonalServer cluster nodes must follow git version control:

1. **Development & Commit**:
   - Write and test code locally on the current branch (e.g. `feature/distributed-job-scheduler`).
   - Stage and commit code changes with clear commit messages.
   - Push to GitHub: `git push origin <branch>`.

2. **Cluster Node Updates**:
   - On each target node (Vivo Y31, Realme 5 Pro, Windows Worker):
     - Update via git: `git pull origin <branch>` (or `git fetch origin && git checkout <branch> && git reset --hard origin/<branch>` if tracking cleanly).
     - Never use manual SCP to copy repository source files (`services/`, `agent/`, `controller/`, `common/`, etc.).
   - Local configuration files (`config/*.json`, `.env`) and persistent data (`storage/`, `data/`) remain preserved.

3. **Service Reload**:
   - Restart the target service/daemon after pulling updates.
