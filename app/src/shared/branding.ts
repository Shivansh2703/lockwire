// package.json's build metadata owns the approved local identity.
import config from "../../package.json" with { type: "json" };

export const PRODUCT_NAME = config.build.productName;
export const BUNDLE_ID = config.build.appId;
