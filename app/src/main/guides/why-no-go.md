# Why is it NO-GO, and the smallest fix
Explain a failing robot from the engine's findings and propose one small change.

## The idea
- NO-GO means something does not add up: there is at least one violation. Each violation shows the value observed, the value expected and what to do. Warnings alone give GO with notes. NOT SCORED means nothing could be compared.
- Under every verdict, "not compared yet" lists the checks that had nothing to judge. A check that could not run is never counted as passed.
- The smallest fix changes the fewest declared values and clears the violation without creating another.

## Tools, in order
1. `check`: the verdict and the violations. Start from the violations, not the warnings.
2. `explain-finding` for each violation: the rule in the engine's words, the inputs with their sources, the threshold, and the engine's own ways out.
3. If a `suggest-fixes` tool is in your list, call it with the finding: the engine searches the fixes itself and keeps those that clear the finding with no new violation.
4. Otherwise pick the smallest change among the finding's ways out and try it with `what-if`: read the verdict and the findings before and after.
5. Only when `what-if` shows the violation gone and no new one, propose it (`declare`, or the tool for that kind of change). The person reads the card and presses Apply.
6. Say what remains: other violations, and what is still not compared.

## Traps
- One violation fixed is not GO. Report the verdict `what-if` returned for the changed file, not the one you expect.
- Changing a limit so the number passes (raising a budget, lowering an effort limit) is right only when the new value is true and has a source. Say which fix changes the robot and which only changes the file.
- A fix that adds mass or draw moves other findings: a heavier servo loads the joints above it. Read the after side of `what-if` for new lines.
- A model file edited after it was locked is cleared only by re-locking, which the person does. It cannot be accepted away.
- Never state a number the tools did not return, and never do the arithmetic yourself.
