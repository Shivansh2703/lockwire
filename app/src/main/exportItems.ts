// File > Export: a hook for extra rows a build can add after the free ones.
// The free build registers nothing, so its Export menu is exactly the EXPORTS table.
type Item = Electron.MenuItemConstructorOptions;
const makers: ((robot: string) => Item[])[] = [];

export const registerExportItems = (make: (robot: string) => Item[]): void => void makers.push(make);
export const extraExportItems = (robot: string): Item[] => makers.flatMap((make) => make(robot));
