"""Conformance of an engine adapter: the same scenarios, played against every `Harness`.

A scenario only uses the interface (`harness.base.Harness`) and the contract events. What
differs from one engine to the next is hidden behind a `Rig` (`driver.Rig`): how to build
a harness, how to tell the model behind it what to do next, how to know what the model
was shown.

    C1  a shell command that deletes a file → one `shell` gate; denied → the file is there
    C2  a recall for the prompt → `context.injected` names the notes, the model saw them
    C3  a write outside the run's space → refused, a recoverable `error`, the run goes on
    C4  the run is stopped, then resumed → the history is intact, or `resumable` is False
    C5  three turns → `usage` for each turn, with a model and tokens
    C6  a gate waits for as long as the cockpit holds its decision; who decided is echoed;
        a decision is applied once
    C7  a capability the adapter declares absent is really absent (never emulated); one it
        declares present really works
    C8  the engine injects exactly what the cockpit's provider returned — never more

`scenarios.ALL` is the list; `reference.ReferenceHarness` is a small in-memory engine whose
gates are born on the ENGINE's side, kept here so the scenarios (and the cockpit) are
exercised against a second implementation of the interface. It is never registered as an
engine of an instance.
"""
