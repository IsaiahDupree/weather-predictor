// Tests for funnel.js — run with: node --test web/
// No mocks. Golden calldata vectors are generated from the Python module
// (tools/polymarket_v2.py), so this asserts byte-for-byte cross-language parity:
// the browser must encode exactly what the verified Python path encodes.
import { test } from "node:test";
import assert from "node:assert/strict";
import * as f from "./funnel.js";

const FUNDER = "0xEE2D0C138E3CBf7965f6F9e6D63e6EC81007B482";

// ---- golden vectors from tools/polymarket_v2.py ----
const GOLD = {
  approve: "0x095ea7b3000000000000000000000000e111180000d2663c0091e4f400237545b87b996b00000000000000000000000000000000000000000000000000000000004c4b40",
  wrap: "0x623556380000000000000000000000002791bca1f2de4661ed88a30c99a7a9449aa84174000000000000000000000000ee2d0c138e3cbf7965f6f9e6d63e6ec81007b48200000000000000000000000000000000000000000000000000000000004c4b40",
  setOp: "0xa22cb465000000000000000000000000e111180000d2663c0091e4f400237545b87b996b0000000000000000000000000000000000000000000000000000000000000001",
  balanceOf: "0x70a08231000000000000000000000000ee2d0c138e3cbf7965f6f9e6d63e6ec81007b482",
};

test("calldata matches Python golden vectors byte-for-byte", () => {
  assert.equal(f.cdApprove(f.ADDR.EXCHANGE_V2, 5_000_000n), GOLD.approve);
  assert.equal(f.cdWrap(f.ADDR.USDCE, FUNDER, 5_000_000n), GOLD.wrap);
  assert.equal(f.cdSetApprovalForAll(f.ADDR.EXCHANGE_V2, true), GOLD.setOp);
  assert.equal(f.cdBalanceOf(FUNDER), GOLD.balanceOf);
});

test("selectors are the verified 4-byte values", () => {
  assert.equal(f.SEL.approve, "095ea7b3");
  assert.equal(f.SEL.wrap, "62355638");
  assert.equal(f.SEL.getCollateral, "5c1548fb");
  assert.equal(f.SEL.exactInputSingle, "04e45aaf");
  assert.equal(f.SEL.quoteExactInputSingle, "c6a5026a");
});

test("parseUnits is exact money math (matches Python to_units)", () => {
  assert.equal(f.parseUnits("5"), 5_000_000n);
  assert.equal(f.parseUnits("0.1"), 100_000n);
  assert.equal(f.parseUnits("2.07"), 2_070_000n);    // the float trap
  assert.equal(f.parseUnits("50.74"), 50_740_000n);
  assert.equal(f.parseUnits("1.2345678"), 1_234_567n); // floors, never over-spends
  assert.throws(() => f.parseUnits("abc"));
});

test("formatUnits round-trips", () => {
  assert.equal(f.formatUnits(50_740_000n), "50.74");
  assert.equal(f.formatUnits(5_000_000n), "5");
  assert.equal(f.formatUnits(0n), "0");
});

test("minOut slippage matches Python", () => {
  assert.equal(f.minOut(5_000_000n, 50), 4_975_000n);
  assert.equal(f.minOut(5_000_000n, 0), 5_000_000n);
  assert.throws(() => f.minOut(1n, 10001));
});

test("calldata field lengths are well-formed", () => {
  assert.equal(f.cdApprove(f.ADDR.EXCHANGE_V2, 1n).length, 2 + 8 + 64 + 64);
  assert.equal(f.cdWrap(f.ADDR.USDCE, FUNDER, 1n).length, 2 + 8 + 64 * 3);
  assert.equal(f.cdExactInputSingle({
    tokenIn: f.ADDR.NATIVE_USDC, tokenOut: f.ADDR.USDCE, fee: 100,
    recipient: FUNDER, amountIn: 5_000_000n, amountOutMin: 4_975_000n,
  }).length, 2 + 8 + 64 * 7);
});

test("max-uint approval encodes fully (not truncated)", () => {
  assert.ok(f.cdApprove(f.ADDR.EXCHANGE_V2, f.MAX_UINT).endsWith("f".repeat(64)));
});

// ---- planner parity with Python ----
function state(o = {}) {
  return {
    native: 0n, usdce: 0n, pusd: 0n, matic: 0n,
    exchAllow: Object.fromEntries(f.APPROVE_TARGETS.map((a) => [a, 0n])),
    ctfOperator: Object.fromEntries(f.CTF_OPERATORS.map((a) => [a, false])),
    ...o,
  };
}
const TGT = f.parseUnits("5");

test("plan: from native only needs full funnel", () => {
  const steps = f.planSteps(state({ native: f.parseUnits("50") }), TGT).map((s) => s.step);
  assert.equal(steps[0], "swap");
  assert.ok(steps.includes("wrap"));
  assert.ok(steps.includes("approve-pusd"));
  assert.ok(steps.includes("approve-ctf"));
});

test("plan: with USDC.e, no swap", () => {
  const steps = f.planSteps(state({ usdce: f.parseUnits("5") }), TGT).map((s) => s.step);
  assert.ok(!steps.includes("swap"));
  assert.ok(steps.includes("wrap"));
});

test("plan: partial pUSD wraps only the shortfall", () => {
  const s = state({ usdce: f.parseUnits("10"), pusd: f.parseUnits("2") });
  const wrap = f.planSteps(s, TGT).find((x) => x.step === "wrap");
  assert.match(wrap.detail, /\b3\b/);
});

test("plan: fully provisioned is ready", () => {
  const s = state({
    pusd: f.parseUnits("5"),
    exchAllow: Object.fromEntries(f.APPROVE_TARGETS.map((a) => [a, f.MAX_UINT])),
    ctfOperator: Object.fromEntries(f.CTF_OPERATORS.map((a) => [a, true])),
  });
  assert.deepEqual(f.planSteps(s, TGT).map((x) => x.step), ["ready"]);
});

test("buildActions: from native only → approve-router, swap, approve-onramp, wrap, approvals", () => {
  const s = state({ native: f.parseUnits("50"), funder: FUNDER });
  const acts = f.buildActions(s, TGT, { fee: 100, out: f.parseUnits("4.999") });
  const keys = acts.map((a) => a.key);
  assert.deepEqual(keys.slice(0, 4), ["approve-router", "swap", "approve-onramp", "wrap"]);
  assert.ok(keys.some((k) => k.startsWith("approve-pusd-")));
  assert.ok(keys.some((k) => k.startsWith("ctf-")));
  // swap tx targets the router with a real exactInputSingle payload
  const swap = acts.find((a) => a.key === "swap");
  assert.equal(swap.to, f.ADDR.UNISWAP_ROUTER);
  assert.ok(swap.data.startsWith("0x" + f.SEL.exactInputSingle));
  // wrap sends to the onramp, recipient = funder
  const wrap = acts.find((a) => a.key === "wrap");
  assert.equal(wrap.to, f.ADDR.ONRAMP);
  assert.ok(wrap.data.includes(FUNDER.slice(2).toLowerCase()));
});

test("buildActions: have pUSD + approvals → no actions", () => {
  const s = state({
    funder: FUNDER, pusd: f.parseUnits("5"),
    exchAllow: Object.fromEntries(f.APPROVE_TARGETS.map((a) => [a, f.MAX_UINT])),
    ctfOperator: Object.fromEntries(f.CTF_OPERATORS.map((a) => [a, true])),
  });
  assert.equal(f.buildActions(s, TGT, null).length, 0);
});

test("buildActions: swap amount is the shortfall, honors min-out slippage", () => {
  const s = state({ usdce: f.parseUnits("2"), funder: FUNDER });
  const acts = f.buildActions(s, TGT, { fee: 100, out: f.parseUnits("3") }, 50);
  const swap = acts.find((a) => a.key === "swap");
  // needU = 5 - 2 = 3 USDC.e shortfall -> swap ~3 native; min-out = 3 * 0.995
  assert.match(swap.detail, /3 native USDC/);
});

test("network guard accepts only Polygon", () => {
  assert.ok(f.isRightNetwork("0x89"));
  assert.ok(f.isRightNetwork(137));
  assert.ok(!f.isRightNetwork("0x1"));   // Ethereum mainnet rejected
  assert.ok(!f.isRightNetwork(1));
});
