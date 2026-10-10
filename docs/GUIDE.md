# Step-by-step guide: Deliverable 02 (Group G18)

This guide takes you from "unzipped folder" to "submitted report". It is
written for an **HP 348 laptop** (Intel Core i5, 4 cores / 8 threads, 8 GB
RAM) running **Windows 10/11**. If you use Ubuntu directly, skip step 1.

| Step | What | Time |
|---|---|---|
| 0 | Read what is in the package | 10 min |
| 1 | Install WSL2 + Ubuntu (Windows only) | 20 min |
| 2 | Install Docker, kind, kubectl, Python | 20 min |
| 3 | Run the simulator (no Docker needed) | 5 min |
| 4 | Real-process check (Docker only, no Kubernetes) | 5 min |
| 5 | Smoke test of the Kubernetes testbed | 40 min |
| 6 | Real testbed run (`quick` profile) | ~5 h, unattended |
| 7 | Analyse and rebuild the PDF | 10 min |
| 8 | Submit + push to GitHub | 10 min |

**Minimum to submit:** steps 0, 3 and 7. The report already contains the
simulator results, and every testbed cell says *pending*.

**Strongly recommended:** steps 5 and 6, so that the testbed columns hold
real numbers. The instructor asked for a *working implementation* and
*reproduced results*, and the testbed is what shows that.

---

## 0. What is in the package

```
g18/
├── report/            LaTeX report (main.tex, references.bib, figures/, generated/)
│   └── main.pdf       compiled report (simulator results; testbed = "pending")
├── src/               Go code: sut (service), loadgen, ski (SKI emulation), governor
├── manifests/         Kubernetes YAML (repro = Roberts et al., overload = our study)
├── cluster/           kind cluster configs (single node = default)
├── experiments/       all scripts: 00_setup ... 05_stall, run_all.sh, config.env
├── sim/               discrete-event simulator + run_all.py
├── analysis/          analyze_testbed.py  (raw runs -> tables/figures for the report)
├── results/           sim/ (done), local/ (done), testbed/ (you create it)
└── docs/              this guide, VIVA_NOTES.md, cutoff1/ (old proposal + old code)
```

**Decision on the base paper.** We **keep** Roberts et al. (arXiv
2507.02158). See `docs/VIVA_NOTES.md` for the reasons, written so you can
say them out loud.

---

## 1. Windows only: install WSL2 + Ubuntu

1. Open **PowerShell as Administrator** and run:
   ```powershell
   wsl --install -d Ubuntu-22.04
   ```
   Restart when asked. When Ubuntu opens, choose a username and password.
2. Give WSL enough memory. In Windows, create the file
   `C:\Users\<you>\.wslconfig` with this content:
   ```ini
   [wsl2]
   memory=6GB
   processors=8
   swap=4GB
   ```
   Then run `wsl --shutdown` in PowerShell and open Ubuntu again.
3. **Copy the project into the Linux home folder.** Do not run it from
   `/mnt/c/...`, because that is slow and can break line endings. In
   Ubuntu:
   ```bash
   cp /mnt/c/Users/<you>/Downloads/G18_Deliverable02.zip ~ && cd ~
   sudo apt update && sudo apt install -y unzip
   unzip G18_Deliverable02.zip && cd g18
   ```

## 2. Install the tools (inside Ubuntu)

**Docker.** Pick ONE option:

- *Option A (easiest on Windows):* install **Docker Desktop**. Then go to
  Settings → Resources → WSL integration and turn on Ubuntu-22.04.
  Leave Docker Desktop running.
- *Option B (pure Ubuntu, uses less RAM):*
  ```bash
  curl -fsSL https://get.docker.com | sudo sh
  sudo usermod -aG docker $USER && newgrp docker
  sudo service docker start      # in WSL, run this after every restart
  ```

**kind, kubectl, Python packages:**
```bash
# kind v0.23.0 (the version our node image is pinned to)
curl -Lo kind https://kind.sigs.k8s.io/dl/v0.23.0/kind-linux-amd64
chmod +x kind && sudo mv kind /usr/local/bin/

# kubectl 1.27 (matches the cluster)
curl -LO https://dl.k8s.io/release/v1.27.13/bin/linux/amd64/kubectl
chmod +x kubectl && sudo mv kubectl /usr/local/bin/

# Python + matplotlib (for simulator and analysis)
sudo apt install -y python3 python3-pip
pip3 install matplotlib        # if pip complains: pip3 install --break-system-packages matplotlib
```

Check that everything works:
```bash
docker run --rm hello-world && kind version && kubectl version --client && python3 --version
```

You do **not** need to install Go. Docker builds the Go code for you.

## 3. Run the simulator (5 minutes, works on any laptop)

```bash
python3 sim/run_all.py
```
This regenerates everything under `results/sim/`, `report/figures/sim_*`
and `report/generated/sim_*`. The numbers come out identical every time,
because all runs are seeded. Use `--fast` for a 1-minute version while
testing.

## 4. Real-process check (5 minutes, Docker only)

This shows the core mechanism on real processes, without Kubernetes.
```bash
docker build -t g18/tools:latest src/
python3 experiments/local_check.py --docker --cpus 0.5
```
You should see `/healthz` failing the 1 s timeout at 150 % load while
`/livez` answers in a few milliseconds, and `/livez` turning to 503
about 5 s after the hang. The script writes
`report/generated/local_numbers.tex`, replacing the numbers measured on
our machine with yours.

## 5. Smoke test of the Kubernetes testbed (~40 min)

```bash
PROFILE=smoke bash experiments/run_all.sh
```
This creates the cluster, builds and loads the images, deploys the
SockShop database, and runs one short run of each experiment. Results go
to `results/testbed-smoke/` and **never** into the report. Check that:
- it finishes without `ERROR`;
- `results/testbed-smoke/preview/` contains `tb_*.tex` files;
- `results/testbed-smoke/calibration/CAPACITY` holds a number (typically
  10–30 req/s).

Useful commands while it runs, in a second Ubuntu window:
```bash
kubectl get ns                       # one namespace per running experiment
kubectl get pods -A | grep g18       # pods of the current run
kubectl get events -n <namespace>    # see liveness kills live
```

## 6. Real run for the report (`quick` profile, ~5 hours)

Before you start:
- plug in the charger;
- set Windows **Sleep = Never** (Settings → System → Power);
- close Chrome and other heavy programs.

```bash
PROFILE=quick bash experiments/run_all.sh 2>&1 | tee results/testbed_run.log
```
- **You can stop and resume.** Press Ctrl+C at any time and run the same
  command again later. Finished runs are skipped.
- **Want the paper-quality version later (Cutoff 3)?**
  `PROFILE=full bash experiments/run_all.sh` takes about 10 hours
  (overnight).
- **Only one part?** Every experiment has its own script:
  `bash experiments/01_repro.sh`, `02_calibrate.sh`, `03_overload.sh`,
  `03b_spike.sh`, `04_hang.sh`, `05_stall.sh`, `06_sweep.sh` (optional).

## 7. Analyse and rebuild the PDF

```bash
python3 analysis/analyze_testbed.py      # reads results/testbed, writes report/generated/tb_*.tex
```

**Option A: Overleaf (recommended).**
1. Zip the `report/` folder and upload it to your Overleaf project
   (New Project → Upload Project).
2. Set Menu → Compiler → **pdfLaTeX** and click Recompile.

The testbed columns now show your numbers instead of *pending*.

**Option B: locally.**
```bash
sudo apt install -y texlive-latex-extra texlive-publishers texlive-science latexmk
cd report && latexmk -pdf main.tex
```

**After the testbed numbers appear, read the report once.** A few
sentences describe the *simulator* results, and your testbed numbers may
differ slightly. Where they differ, add one sentence such as "On the
testbed, FP restarted X times instead of Y, because ...". If a testbed
result *contradicts* the simulator (for example, A also restarts under
the spike), report it honestly. That is a finding, not a failure.

## 8. Submit

1. `report/main.pdf`, plus the LaTeX source (the `report/` folder), as
   the instructor asked.
2. Push the code and results to your GitHub repository (from Cutoff 1):
   ```bash
   git init  # only if the folder is not a repo yet
   git remote add origin https://github.com/Muhammad-Yasir-Suhail/k8s-liveness-overload-analysis.git
   git add . && git commit -m "Deliverable 02: reproduction, gap, proposed method, results"
   git push -u origin main
   ```
3. Update the Google Sheet row G18 if the instructor asks for links.

---

## Troubleshooting

| Problem | Fix |
|---|---|
| `kind create cluster` fails or hangs | Docker is short of memory. Check `.wslconfig` (6 GB) and close other apps, then `kind delete cluster --name g18` and retry. |
| `toomanyrequests` from Docker Hub | You hit the pull rate limit. Wait 1 h or run `docker login` (a free account doubles the limit). Images are pulled only once. |
| `catalogue image has no /bin/sh` | Very unlikely (image is Alpine-based). Report it in "Problems encountered", then run `R_ARMS="FP" ...` with `catalogue-fp.yaml` edited to drop the wrapper. |
| SKI pod never Ready | `kubectl logs -n <ns> <pod> -c ski`. The sidecar uses a TCP fallback after 3 s, so this usually means RBAC or the API server is slow. Check for `SKI-ERROR`. |
| Calibration warning "capacity may be higher" | Add larger rates to `C_RATES` in `experiments/config.env`, delete `results/testbed/calibration/`, re-run `02_calibrate.sh`. |
| A run hangs | Ctrl+C, then `kubectl get ns \| grep g18-` and `kubectl delete ns <name>`, then start the script again (finished runs are kept). |
| Laptop slept during the run | Delete the folder of the interrupted run (the one without `meta.json`) and start again. |
| `permission denied` running a script | Always start scripts with `bash experiments/...` (Windows unzip drops the executable bit). |
| Everything is broken | `kind delete cluster --name g18` and start from step 5. |

## Time budget for the `quick` profile

| Experiment | Runs | Approx. time |
|---|---|---|
| R reproduction (DP, FP, SKI × 3) | 9 | 90 min |
| C calibration | 10 steps | 12 min |
| O sustained 90 % (7 arms) | 7 | 40 min |
| P load spike (7 arms) | 7 | 40 min |
| H silent hang (6 arms × 2) | 12 | 65 min |
| D dependency stall (6 arms × 2) | 12 | 65 min |
| **Total** | | **≈ 5 h** |
