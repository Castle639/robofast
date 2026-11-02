"""Test and simulation helpers (never imported by a running observatory)."""

#: The optics tests and simulations assume: a 0.7 m telescope with a 47 %
#: central obstruction (a CDK700). Core has no default aperture
#: (docs/design.md 3.10); tests pass this.
TEST_OPTICS = {"aperture": 700.0, "obstruction": 0.47}
