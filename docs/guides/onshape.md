---
title: From Onshape to Lockwire and back
description: Get an Onshape robot into a URDF, check it with Lockwire, and export it back out.
---

# From Onshape to Lockwire and back

Lockwire reads a URDF, so your Onshape model has to be exported to one first.

1. Export a URDF from your Onshape assembly with the community tool
   [onshape-to-robot](https://github.com/Rhoban/onshape-to-robot). Follow its README for
   installing it and for giving it access to your document. That step belongs to the tool;
   Lockwire never talks to Onshape and needs no Onshape keys.
2. Check the model:

   ```bash
   lockwire check robot.urdf
   ```

3. Lock it, so later changes are checked against what you have now:

   ```bash
   lockwire start
   ```

4. Export it back out through the MCP `export_model` tool (your AI agent calls it): URDF,
   SDF or MJCF, free. A ROS 2 description package (package.xml, `urdf/`, `meshes/`, `launch/`)
   is a Pro feature.
