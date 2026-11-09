---
description: Robot model checks with Lockwire. Use when editing a URDF, xacro, SDF or lockwire.yaml, or before commanding a robot to move.
---

Lockwire holds the physics a robot was locked with (masses, inertias, joint limits,
power, torque) in `lockwire.yaml`, and checks every change against it.

## Before you edit a model file

Run `lockwire check lockwire.yaml` (or `lockwire check <model.urdf>` if there is no
lockfile yet). Read the verdict: GO, NO-GO, REFUSED or NOT SCORED.

The edit hook checks your Write/Edit too. If it blocks you, it prints what broke and
the command that would re-lock. Do not run that command yourself: undo the edit, or
tell the human what changed and let them decide.

## Before any motion

Write the motion as a plan and run `lockwire plan lockwire.yaml plan.yaml`. Only a GO
plan may run.

## Never change a declared value without a human witness

A declared value (a datasheet mass, a measured torque) is evidence a person stood
behind. Change one only when a human gives you the new value and their name:
`lockwire declare lockwire.yaml <quantity> "<value>" --source "<where>" --witness "<name>"`.
Never pick a witness name yourself, and never re-lock (`lockwire start ... --force`)
to make a check pass.

## For a review

Hand the robot-reviewer agent the repo; it runs `lockwire check --json -` and reports.
