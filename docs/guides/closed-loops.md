---
title: Closed loops
description: Which closed linkages Lockwire scores the holding torque of, and which it says plainly it does not score yet.
---

# Closed loops

A closed loop is a linkage whose links join back on themselves, such as a five-bar leg. Lockwire
scores the holding torque of its driven joints by solving the loop at each pose. It reads the pins
from an SDF file; a URDF cannot close a loop.

## What a loop must look like to be scored

- The model is an SDF file, found on disk beside the lock.
- Every joint in the loop is a revolute pin, and all their axes are parallel (a flat loop).
- The loop's base is fixed: no moving joint above it.
- Nothing moves below the loop: no serial joint after its output link.
- The driven joints (a positive effort limit) are exactly as many as the loop's freedoms: a
  five-bar has two. Every free pin has effort 0 or none, including the pin that closes the loop.
- One link into the loop's closing end sits at its own origin (no `<pose>`); the closing joint
  carries the pin position in its `<pose>`. Every other joint has no `<pose>`.
- Loops that are separate, or that share links, are each solved. Up to four driven joints on one
  loop are scored on a coarse grid and the output says so.

## What is not scored yet

Each of these gets no torque number, one plain line that names the joints and the reason, and
the same reason in `--json`. `swap`, `what-if` and the standing load lines of `check` give no number
for those joints either.

- A loop that hangs from a moving joint, or has a joint hanging below it. To check the loop
  alone, make that joint a fixed joint in a copy of the file.
- A loop that is not flat, or has a sliding, ball or fixed joint in it.
- More or fewer driven joints than the loop has freedoms, or a driven closing pin.
- A loop with no driven joint (no effort limit anywhere in it).
- A loop with more than four driven joints.
- A loop that cannot close anywhere in the driven joints' ranges.
- A joint with a `<pose>` of its own away from its child link, in a file that has a loop.
- A MuJoCo (MJCF) model with an `<equality>` constraint (connect, weld or joint) that closes a loop
  or couples joints. Lockwire solves loops only from an SDF, so the joints of that part of the
  model (the whole piece the constraint touches) get no number from the gravity reach, the hold
  torque at a plan pose, the move torques in the app and the material comparison. The gravity
  reach and the hold torque each say so in a line of their own; the others leave those joints out.
  A free arm in the same file keeps its numbers.
  Lockwire finds the MJCF file from the lock's own folder, so the answer is the same whichever
  folder you run the command from. If the file is not there, Lockwire cannot see the constraint:
  it scores those joints as if nothing tied them, and their numbers are wrong. Keep the model
  file where the lock names it. Write the model as an SDF to score a loop.
