Frozen routing fixture from Project Room commit `a797bf7a3f327c0bd9ab2001caca154ff48c67ef`, used only with a synthetic temporary room.

`guard.py` is the exact guard file from that commit. `fixture.json` contains its exact matcher, settings template, agent definitions, public provider-none policy and one-time workflow parts. Only temporary runtime paths and the guard command placeholder are substituted by the regression. Tests never read Git history or invoke Claude inference.
