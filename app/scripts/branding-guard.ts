type BuildBranding = {
  productName?: unknown;
  appId?: unknown;
};
type RuntimeBranding = {
  PRODUCT_NAME: string;
  BUNDLE_ID: string;
};

/** Packaging must fail before build/staging if identity is unresolved or split. */
export function assertPackagingBranding(
  build: BuildBranding,
  runtime: RuntimeBranding,
): void {
  const { productName, appId } = build;
  if (
    typeof productName !== "string" ||
    !productName.trim() ||
    productName !== productName.trim() ||
    /[\\/]/.test(productName) ||
    typeof appId !== "string" ||
    !/^[a-zA-Z][a-zA-Z0-9-]*(\.[a-zA-Z][a-zA-Z0-9-]*)+$/.test(appId)
  ) {
    throw new Error("packaging branding is missing or malformed");
  }
  const placeholder = /placeholder|provisional|your[ ._-]?(company|app)|example/i;
  if (placeholder.test(productName) || placeholder.test(appId)) {
    throw new Error("packaging branding contains a placeholder");
  }
  if (productName !== runtime.PRODUCT_NAME || appId !== runtime.BUNDLE_ID) {
    throw new Error("packaging branding differs from runtime branding");
  }
}
