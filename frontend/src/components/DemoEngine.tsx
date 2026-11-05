import { useEffect, useState } from "react";
import { Cpu, X } from "lucide-react";
import { currentModelState, loadModel, modelAvailable, recording, stopModel, watchModel, type ModelState } from "../demo/replay";

const MODEL_NAME = "Qwen3-0.6B";
const MODEL_SIZE = "约 350 MB";

/** Which engine wrote this answer, on every answer of the static demo. None of them is a claim about model quality. */
export function EngineTag({ engine }: { engine?: string }) {
  if (engine === "browser-rules") {
    return <span className="badge engine-tag" title="由本页面里的规则、检索和工具回答，数据为虚构；不是模型，也不代表真实模型的质量。">规则引擎 · 模拟</span>;
  }
  if (engine === "qwen3-0.6b-webgpu") {
    return <span className="badge engine-tag" title="模型只选择意图、订单和依据，事实由程序陈述，动钱仍由规则决定；不代表真实模型的质量。">{MODEL_NAME} · 浏览器内 · 模拟</span>;
  }
  const rec = recording();
  const model = rec && !rec.model.offline ? rec.model.strong : "离线模型";
  return <span className="badge engine-tag" title={rec ? `${rec.date} 录制自真实后端` : "录制自真实后端"}>录制 · {model}</span>;
}

/** The click that loads the model: offered only where it can run, and nothing is downloaded before it. */
export function DemoEngineBar() {
  const [available, setAvailable] = useState(false);
  const [state, setState] = useState<ModelState>(currentModelState());
  useEffect(() => {
    let live = true;
    modelAvailable().then((yes) => { if (live) setAvailable(yes); }).catch(() => undefined);
    const stop = watchModel(setState);
    return () => { live = false; stop(); };
  }, []);
  if (!available && state.status === "off") return null;
  return (
    <div className="demo-engine" role="status">
      <Cpu size={14} aria-hidden="true" />
      {state.status === "off" && (
        <>
          <span className="demo-engine-text">当前由规则引擎回答。可以在这台电脑上加载开源模型 {MODEL_NAME}（{MODEL_SIZE}，在浏览器里运行，点击后才下载）。</span>
          <button className="btn btn-sm" onClick={() => void loadModel()}>加载模型</button>
        </>
      )}
      {state.status === "loading" && (
        <>
          <span className="demo-engine-text">正在加载 {MODEL_NAME}，规则引擎继续回答。{state.text}</span>
          <div className="demo-engine-bar" role="progressbar" aria-label="模型加载进度" aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(state.progress * 100)}>
            <span style={{ width: `${Math.round(state.progress * 100)}%` }} />
          </div>
          <button className="btn btn-sm" onClick={stopModel}><X size={14} aria-hidden="true" /> 取消</button>
        </>
      )}
      {state.status === "ready" && (
        <>
          <span className="demo-engine-text">{MODEL_NAME} 已加载：模型只选择意图、订单和依据，程序校验后陈述事实，动钱仍由规则决定。不代表真实模型的质量。</span>
          <button className="btn btn-sm" onClick={stopModel}>卸载模型</button>
        </>
      )}
      {state.status === "failed" && (
        <>
          <span className="demo-engine-text">模型没有加载成功，继续由规则引擎回答：{state.error}</span>
          <button className="btn btn-sm" onClick={() => void loadModel()}>重试</button>
        </>
      )}
    </div>
  );
}
