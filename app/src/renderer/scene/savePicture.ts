/** Save picture: the 3D view as a PNG (base64, no prefix), read from the live canvas. The renderer keeps its drawing buffer, so this works between frames. */
export function canvasPng(canvas: HTMLCanvasElement): string {
  return canvas.toDataURL("image/png").split(",")[1] ?? "";
}

/** Pictures the first 3D view on the page (the one the View menu belongs to) as <name>.png via the save dialog. Never touches the lockfile. */
export async function savePicture(name: string): Promise<void> {
  const canvas = document.querySelector<HTMLCanvasElement>(".shell-scene canvas");
  if (canvas) await window.lockwire.savePicture(name, canvasPng(canvas));
}
