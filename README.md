# looptap

Watch and steer a running training loop from your terminal.

`looptap` pairs a lightweight library for your training script with a terminal UI (TUI) that attaches to it when you want a look. Your script streams metrics to a local socket; whenever you want to check in, you attach the TUI to watch live plots, tweak parameters, pause, trigger actions, or stop for a checkpoint and resume later. Detach whenever you're done, and training keeps rolling as if you never opened the UI.

Your training loop doesn't depend on the TUI being there. It runs the same with nothing attached, over SSH, or under a job scheduler, and you can attach and detach as often as you like.

## Features

- **Live metrics:** Values, rates, ETAs, line plots, bar charts, and histograms, laid out the way your script defines them.
- **Live parameters:** Change values mid-run from the TUI, checked against the types and bounds your script declares.
- **Custom actions:** Trigger things your script defines—like saving a sample or running a quick validation pass—right from the TUI.
- **Pause and resume:** Pause at a safe point your training loop chooses, then resume when you're ready.
- **Stop and resume:** Stop a run with a checkpoint to free up the GPU, then resume it later from the TUI or CLI with plots picking up where they left off.
- **Run picker:** See every run in one place—live, stopped, finished, or crashed—along with its latest values.
- **Zero infrastructure:** Just local files and a Unix socket per run. No servers, daemons, or accounts.

## Installation

`looptap` isn't on PyPI yet, so install it from GitHub:

```bash
uv add "looptap[tui] @ git+https://github.com/<your-username>/looptap"
```

The `tui` extra brings in the terminal UI dependencies ([Textual](https://textual.textualize.io/) and [textual-plotext](https://github.com/Textualize/textual-plotext)). If a machine only runs training, plain `looptap` is enough.

Requires Python 3.10+ on Linux or macOS.

## Quick start

```python
import time

from looptap import Param, Plot, Rate, Value, run


def train(ctl):
    for step in range(10_000):
        for action in ctl.poll():  # applies TUI commands; blocks while paused
            if action == "save":
                print("saving")

        time.sleep(ctl.params.step_delay)  # stand-in for a training step
        loss = 1 / (step + 1)

        if step % 25 == 0:
            ctl.report({"step": step, "loss": loss})


if __name__ == "__main__":
    run(
        train,
        metrics=["step", "loss"],
        views=[
            Value("step"),
            Rate("step", label="steps/s"),
            Plot(x="step", y="loss", color="cyan"),
        ],
        params=[Param("step_delay", float, default=0.02, min=0, max=1)],
        actions=["save"],
        pausable=True,
    )
```

Start the script in one terminal, then attach from another:

```bash
python train.py
looptap
```

Running `looptap` opens the run picker—hit Enter on a run to attach. You can also jump straight to one with `looptap runs/<run>`.

## Writing a training script

Everything runs through `run()` and the controller passed into your training function (usually named `ctl`).

### Metrics and views

Pass everything your loop emits to `metrics=`, and send updates with `ctl.report()`. Treat your step counter like any other metric. Values can be numbers, 0-d tensors, or 1-D lists or tensors (for vector metrics). Since turning a CUDA tensor into Python data synchronizes with the GPU, call `ctl.report()` at points where you'd synchronize anyway, like a logging step.

`views=` controls what shows up in the TUI. Each view names the metrics it reads, and `run()` validates those names at startup so a typo fails fast instead of leaving you with a blank panel.

| View | What it shows |
|---|---|
| `Value(metric, label=None)` | The latest value. |
| `Rate(metric, label=None, window=1)` | How fast a metric changes per second, averaged over `window` report intervals. |
| `ETA(metric, total, label=None, window=10)` | Estimated time remaining until `metric` hits `total`, plus an approximate finish time. `total` can be a number or another metric's name. |
| `Plot(x, y, title=None, window=None, color=None)` | `y` versus `x`. Set `window` to show only the latest points, or `None` for the full run. |
| `Bars(metric, labels=None, title=None, color=None)` | One bar per element from the latest value of a vector metric. |
| `Histogram(metric, bins=20, title=None, limits=None, density=False, color=None)` | Distribution of the latest value of a vector metric. `bins` can be a count or a list of edges (unequal widths work too); `limits=[low, high]` sets equal-width bin bounds; `density` normalizes by bin width. |

`Value`, `Rate`, and `ETA` sit in a top row, with plots and charts stacked underneath. Colors accept terminal palette names (`red`, `cyan+`, …) or `"#rrggbb"` hex strings.

### Params, actions, and pausing

Parameter updates, actions, and pauses take effect when your loop calls `ctl.poll()`. Call it once per iteration at a point where mid-loop changes are safe. If you declare params, actions, or pausing, make sure your loop calls `ctl.poll()`—otherwise commands just sit in the queue unapplied.

- **`params=[Param(name, type, default, min=None, max=None)]`** declares parameters you can edit live in the TUI. Read them via `ctl.params.<name>`. Because updates only happen inside `ctl.poll()`, values stay consistent throughout the rest of the iteration. Supported types are `int`, `float`, `bool`, and `str`.
- **`actions=["save", ...]`** declares actions you can trigger from the TUI. `ctl.poll()` yields the names of any actions triggered since the previous call.
- **`pausable=True`** lets you pause the run from the TUI. While paused, `ctl.poll()` blocks, and parameter edits or actions are rejected until you resume.

The training worker always has the final say: the TUI only reflects a change once the worker confirms it, and every command gets either a confirmation or a clear reason why it was rejected.

### Stop and resume

To support stopping and resuming, register a save and load hook with the controller before entering your loop:

```python
def train(ctl):
    model, optimizer = build()

    def save(directory):
        torch.save(
            {
                "model": model.state_dict(),
                "optimizer": optimizer.state_dict(),
                "step": step,
            },
            directory / "train.pt",
        )

    def load(directory):
        state = torch.load(directory / "train.pt")
        model.load_state_dict(state["model"])
        optimizer.load_state_dict(state["optimizer"])
        return state["step"]

    start = ctl.checkpointing(save, load) or 0
    for step in range(start, total_steps):
        ctl.poll()
        ...
```

When you stop a run from the TUI, `ctl.poll()` calls `save` with a temporary directory, records the current parameter values, atomically swaps the checkpoint into place, and exits the process. If something crashes mid-save, your previous checkpoint stays untouched. If `save` raises an exception, `looptap` surfaces the error and lets training keep going.

When resuming, `checkpointing()` calls `load` right away and returns whatever `load` returns (here, the step to pick back up from). On a fresh start it returns `None`; you can also check `ctl.resuming`.

Resume a stopped run from the TUI's Resume button, or run the command printed when the run stopped:

```bash
python train.py --resume runs/<run>
```

### Running modes

`run()` parses its own CLI flags and passes everything else through to your script:

| Flag | What it does |
|---|---|
| *(none)* or `--worker` | Creates a run directory and serves the run over its socket. |
| `--headless` | Runs without `looptap` tracking—no run directory or socket, and params stay at their defaults. |
| `--resume DIR` | Resumes a stopped run from its checkpoint. |

Both `SIGTERM` and Ctrl+C shut the worker down cleanly.

## The TUI

| Screen | Key | Action |
|---|---|---|
| Run picker | `Enter` | Attach to the selected run. |
| Run picker | `r` | Refresh the run list (it also auto-refreshes every two seconds). |
| Run view | `Esc` | Return to the run picker; the run keeps going. |
| Run view | `p` | Pause or resume, if the run is pausable. |
| Anywhere | `q` | Quit the TUI; active runs keep going. |

The controls panel in the run view includes the pause toggle, inputs for each declared parameter, buttons for each action, and a Stop button once the run is resumable (it takes two presses so you don't stop a run by accident).

Run statuses in the picker:

| State | Meaning |
|---|---|
| `live` | Currently running. |
| `stopped` | Stopped cleanly from the TUI with a checkpoint. |
| `finished` | Your training function returned normally. |
| `crashed` | Your training function raised an unhandled exception. |
| `interrupted`, `terminated`, `exited` | Stopped via Ctrl+C, `SIGTERM`, or `sys.exit()`. |
| `died` | Killed without cleanup (for example, `SIGKILL` or an out-of-memory kill). |
| `on another host` | Active on a different machine sharing the same run directory. |

Runs with a complete checkpoint also show a resumable indicator.

## How it works

Each run creates a folder under `runs/`:

| File | What's inside |
|---|---|
| `manifest.json` | Declared metrics, views, params, and actions. |
| `metrics.jsonl` | Reported metrics, one JSON object per line. |
| `control.sock` | The worker's Unix socket while running. |
| `status.json` | Current worker state and how the last run ended. |
| `checkpoint/` | Saved loop state from the most recent stop. |
| `launch.json` | Original command line used to launch the run, so the TUI can resume it. |
| `worker.log` | Logs from workers launched via the TUI. |
| `metrics.discarded.jsonl` | Metric lines set aside on resume from after the checkpointed point. |

Background threads handle the Unix socket and talk to the training thread strictly through in-memory queues. Even if the TUI freezes, hangs, or crashes, it won't block or take down your training loop. Messages travel as newline-delimited JSON in both directions—metrics and state out to the TUI, commands back in.

Access to the socket and `launch.json` is restricted to the file owner, and attaching the TUI requires being on the same machine as the run.

## Status

`looptap` is early-stage, so expect the API and the worker/TUI wire protocol to evolve.