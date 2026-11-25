# looptap
 
Watch and steer a running training loop from your terminal.
 
looptap is a small library your training script calls, plus a terminal UI that attaches to the running script. The script streams its metrics to a local socket. When you want to look, you attach the TUI to see live plots, change parameters, pause, trigger actions, or stop the run with a checkpoint and resume it later. When you're done, you detach, and training carries on as if you'd never been there.
 
The script doesn't depend on the TUI. It runs the same with nothing attached, over SSH, or under a scheduler, and you can attach and detach as often as you like.
 
## Features
 
- **Live metrics** as values, rates, ETAs, line plots, bar charts, and histograms, laid out by the training script.
- **Live parameters** the TUI can change mid-run, validated against the types and ranges the script declares.
- **Actions** the script defines, such as "save a sample" or "run validation", triggered from the TUI.
- **Pause and resume** at a point the training loop chooses.
- **Stop and resume**: stop a run with a checkpoint, freeing its GPU, then resume it later from the TUI or the command line. Plots continue where they left off.
- **A run picker** listing every run, whether it's live, stopped, finished, or crashed, with its latest values.

## Installation
 
looptap isn't on PyPI yet. Install it from GitHub:
 
```
uv add "looptap[tui] @ git+https://github.com/<your-username>/looptap"
```
 
The `tui` extra installs the terminal UI's dependencies ([Textual](https://textual.textualize.io/) and [textual-plotext](https://github.com/Textualize/textual-plotext)). On machines that only run training, plain `looptap` is enough.
 
Requires Python 3.10 or later, on Linux or macOS.
 
## Quick start
 
```python
import time
 
from looptap import Param, Plot, Rate, Value, run
 
 
def train(ctl):
    for step in range(10_000):
        for action in ctl.poll():  # applies commands from the TUI; blocks while paused
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
 
Run the script, then attach from another terminal:
 
```
python train.py
looptap
```
 
`looptap` opens the run picker; press Enter on the run to attach. `looptap runs/<run>` attaches to a run directly.
 
## Writing a training script
 
Everything goes through `run()` and the controller it passes to your training function, conventionally called `ctl`.
 
### Metrics and views
 
`metrics=` names everything the loop reports, and `ctl.report()` sends values for any of them. A step counter is reported like any other metric. Values can be numbers, 0-d tensors, or 1-D lists or tensors (vector metrics). Converting a CUDA tensor synchronises with the GPU, so report at points where you'd synchronise anyway, such as a logging step.
 
`views=` decides what the TUI shows. Each view names the metrics it reads, and `run()` checks at startup that they're declared, so a typo fails immediately rather than showing an empty panel.
 
| View | Shows |
|---|---|
| `Value(metric, label=None)` | The latest value. |
| `Rate(metric, label=None, window=1)` | How fast a metric changes per second, averaged over `window` report intervals. |
| `ETA(metric, total, label=None, window=10)` | Time left until `metric` reaches `total`, and roughly when that will be. `total` is a number or the name of a metric. |
| `Plot(x, y, title=None, window=None, color=None)` | `y` against `x`. `window` shows only the latest points; `None` shows the whole run. |
| `Bars(metric, labels=None, title=None, color=None)` | The latest value of a vector metric, one bar per element. |
| `Histogram(metric, bins=20, title=None, limits=None, density=False, color=None)` | The values in the latest value of a vector metric. `bins` is a count or a list of edges, which can be unequal; `limits=[low, high]` fixes the range of equal-width bins; `density` normalises by bin width. |
 
`Value`, `Rate`, and `ETA` appear in a row at the top; the others stack below. Colours are terminal palette names (`red`, `cyan+`, …) or `"#rrggbb"`.
 
### Params, actions, and pausing
 
These all take effect when the loop calls `ctl.poll()`, so call it once per iteration at a point where changes are safe. If you declare any of them, the loop must call it; otherwise commands queue up unapplied.
 
- **`params=[Param(name, type, default, min=None, max=None)]`** declares values the TUI can change. Read them as `ctl.params.<name>`. They only change inside `ctl.poll()`, so they're stable for the rest of the iteration. `type` is `int`, `float`, `bool`, or `str`.
- **`actions=["save", ...]`** declares actions the TUI can trigger. `ctl.poll()` returns the names of any triggered since the last call.
- **`pausable=True`** lets the TUI pause the run. `ctl.poll()` blocks while paused. Param changes and actions are rejected while paused; resume first.
The worker is the authority on every value: the TUI only shows a change once the worker confirms it, and every command gets either a confirmation or an error message explaining why it was rejected.
 
### Stop and resume
 
To make a run resumable, tell the controller how to save and load its state, before the loop starts:
 
```python
def train(ctl):
    model, optimiser = build()

    def save(directory):
        torch.save(
            {
                "model": model.state_dict(),
                "optimiser": optimiser.state_dict(),
                "step": step,
            },
            directory / "train.pt",
        )

    def load(directory):
        state = torch.load(directory / "train.pt")
        model.load_state_dict(state["model"])
        optimiser.load_state_dict(state["optimiser"])
        return state["step"]

    start = ctl.checkpointing(save, load) or 0
    for step in range(start, total_steps):
        ctl.poll()
        ...
```
 
When the TUI stops the run, `ctl.poll()` calls `save` with a temporary directory, records the current param values, swaps the result into place, and exits the process. A crash during a save leaves the previous checkpoint intact. If `save` raises, the error is reported and training carries on.
 
When the run is resumed, `checkpointing()` calls `load` immediately and returns its result, here the step to continue from. On a fresh run it returns `None`. `ctl.resuming` says which it is.
 
Resume a stopped run from the TUI with the Resume button, or from the command line with the command the run prints when it stops:
 
```
python train.py --resume runs/<run>
```
 
### Running modes
 
`run()` reads a few flags of its own and leaves the rest for your script:
 
| Flag | Effect |
|---|---|
| (none) or `--worker` | Create a run directory and serve the run over its socket. |
| `--headless` | Run without looptap: no run directory, no socket. Params keep their defaults. |
| `--resume DIR` | Continue a stopped run from its checkpoint. |
 
`SIGTERM` and Ctrl+C both shut the worker down cleanly.
 
## The TUI
 
| Where | Key | Does |
|---|---|---|
| Run picker | Enter | Open the selected run. |
| Run picker | `r` | Refresh the list (it also refreshes every two seconds). |
| Run view | Escape | Back to the run picker. The run keeps going. |
| Run view | `p` | Pause or resume, if the run is pausable. |
| Anywhere | `q` | Quit. Runs keep going. |
 
The run view's controls panel has the pause button, an input for each param, a button for each action, and a Stop button once the run is resumable. Stop takes two presses.
 
The run picker shows each run's state:
 
| State | Meaning |
|---|---|
| live | Running. |
| stopped | Stopped from the TUI with a checkpoint. |
| finished | The training function returned. |
| crashed | The training function raised an exception. |
| interrupted, terminated, exited | Stopped with Ctrl+C, with `SIGTERM`, or by `sys.exit()`. |
| died | Killed without a chance to clean up, e.g. `SIGKILL` or out of memory. |
| on another host | Running on a different machine sharing the same run directory. |
 
Runs with a complete checkpoint are also marked resumable.
 
## How it works
 
Each run gets a directory under `runs/`:
 
| File | Contents |
|---|---|
| `manifest.json` | What the run declared: metrics, views, params, actions. |
| `metrics.jsonl` | Every report, one JSON object per line. |
| `control.sock` | The worker's Unix socket, while it's running. |
| `status.json` | Whether the worker is running, and how it last ended. |
| `checkpoint/` | The loop's saved state from the last stop. |
| `launch.json` | The command line used to start the run, so the TUI can resume it. |
| `worker.log` | Output of workers started from the TUI. |
| `metrics.discarded.jsonl` | Reports set aside on resume, from after the checkpoint that was resumed. |
 
The worker serves its socket from background threads, which only ever exchange data with the training thread through in-memory queues. A slow, stuck, or crashed TUI can't block or crash training. Messages are JSON, one per line, in both directions: metrics and state go to the TUI, and commands come back.
 
The socket and `launch.json` are only readable by their owner. The TUI needs to be on the same machine as the run to attach.
 
## Status
 
looptap is in early development. The protocol between the worker and the TUI, and the API, may still change.
 