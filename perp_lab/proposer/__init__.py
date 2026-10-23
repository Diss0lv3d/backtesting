"""The proposer: trusted orchestrator code around an untrusted model.

The model reads nothing and runs nothing. Every rule (what it is shown, what is judged, what is written,
which commands start) is enforced by code in this package, never by an instruction to the model. This
package never imports the judge, the holdout, the store or the data layer (tests/test_proposer_boundaries.py),
and only `proc.py` may start a process.
"""
