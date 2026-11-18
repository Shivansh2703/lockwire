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
   lockwire start robot.urdf
   ```

4. Export it back out as a URDF, SDF or MJCF file, free:

   ```bash
   lockwire export lockwire.yaml --format urdf --out exported.urdf
   ```

   Your AI agent can do the same through the MCP `export_model` tool. A ROS 2 description package (package.xml, `urdf/`, `meshes/`, `launch/`)
   is a Pro feature. The URDF carries masses and joints but not visual or collision shapes, and a
   revolute or prismatic joint with no effort or velocity limit is named in the reply, because ROS will not load it until you set them.
