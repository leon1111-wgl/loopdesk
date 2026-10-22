// The iterative jailbreak as a promptfoo custom strategy (backend/app/redteam/build.py points at this file).
//
// promptfoo 0.124.1 maps the "jailbreak" strategy to jailbreak:meta, which runs only on promptfoo's servers.
// This is the iterative jailbreak it replaced, on the provider promptfoo still ships locally
// (promptfoo:redteam:iterative): the attacker model rewrites the attack for numIterations rounds, a judge
// scores each answer, and the best attempt is graded. Attacker and judge are the redteam provider, our relay.
// The same transformation as promptfoo's own addIterativeJailbreaks(…, "iterative").
export default {
  id: "jailbreak-iterative",
  action: async (testCases, injectVar, config) =>
    testCases.map((testCase) => ({
      ...testCase,
      provider: { id: "promptfoo:redteam:iterative", config: { injectVar, ...config } },
      assert: testCase.assert?.map((a) => ({ ...a, metric: a.metric ? `${a.metric}/Iterative` : a.metric })),
      metadata: { ...testCase.metadata, strategyId: "jailbreak", originalText: String(testCase.vars[injectVar]) },
    })),
};
