// The demo's second tier: Qwen3-0.6B through WebLLM, off the page's thread. Loaded only after the visitor clicks.
// The runtime and weights are the shared copies on this origin (one download, one browser cache for every project).
let engine = null;
let busy = false;
const send = (id, type, value) => self.postMessage({ id, type, value });
const SITE = "https://leon1111-wgl.github.io/";

self.onmessage = async ({ data }) => {
  const { id, type } = data;
  if (busy) {
    self.postMessage({ id, type: "error", error: "模型正忙。" });
    return;
  }
  busy = true;
  try {
    if (type === "load") {
      if (!engine) {
        const { config } = data;
        if (![config.model, config.model_lib, config.runtime].every((url) => url.startsWith(SITE))) throw new Error("模型来源配置无效。");
        const { CreateMLCEngine } = await import(config.runtime);
        engine = await CreateMLCEngine(config.model_id, {
          initProgressCallback: (progress) => send(id, "progress", progress),
          appConfig: { model_list: [{ model: config.model, model_id: config.model_id, model_lib: config.model_lib, overrides: { context_window_size: 4096 } }] },
        });
      }
      send(id, "done", true);
    } else if (type === "generate") {
      if (!engine) throw new Error("模型尚未加载。");
      const reply = await engine.chat.completions.create({
        messages: data.messages, stream: false, temperature: 0, max_tokens: 96,
        response_format: { type: "json_object", schema: JSON.stringify(data.schema) }, extra_body: { enable_thinking: false },
      });
      send(id, "done", reply.choices[0]?.message?.content ?? "");
    } else {
      throw new Error("未知模型操作。");
    }
  } catch (error) {
    self.postMessage({ id, type: "error", error: `浏览器模型暂不可用：${String(error?.message || error).slice(0, 300)}` });
  } finally {
    busy = false;
  }
};
