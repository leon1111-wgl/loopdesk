// Vitest: the engine's tests evaluate the policy with the Node build of cedar-wasm (the page loads the web build).
import * as cedar from "@cedar-policy/cedar-wasm/nodejs";
import { useCedar } from "./policy";

useCedar(cedar);
