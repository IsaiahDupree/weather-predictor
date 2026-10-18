// funnel.js — pure logic for the Polymarket V2 self-custody funnel.
// Runs in the browser (ES module) AND under node:test. No deps, no network.
// Selectors are hardcoded (verified against Python keccak) so the browser needs
// no crypto lib; the test suite asserts them against golden values.

export const CHAIN_ID = 137;
export const CHAIN_HEX = "0x89";

export const ADDR = {
  NATIVE_USDC: "0x3c499c542cef5e3811e1192ce70d8cc03d5c3359",
  USDCE:       "0x2791bca1f2de4661ed88a30c99a7a9449aa84174",
  PUSD:        "0xc011a7e12a19f7b1f670d46f03b03f3342e82dfb",
  ONRAMP:      "0x93070a847efef7f70739046a929d47a521f5b8ee",
  CTF:         "0x4d97dcd97ec945f40cf65f87097ace5ea0476045",
  EXCHANGE_V2: "0xe111180000d2663c0091e4f400237545b87b996b",
  NEGRISK_EXCHANGE_V2: "0xe2222d279d744050d28e00520010520000310f59",
  NEGRISK_ADAPTER:     "0xd91e80cf2e7be2e162c6513ced06f1dd0da35296",
  UNISWAP_ROUTER: "0x68b3465833fb72a70ecdf485e0e4c7bd8665fc45",
  UNISWAP_QUOTER: "0x61ffe014ba17989e743c5f6cb21bf9697530b21e",
};

export const SEL = {
  approve:            "095ea7b3",
  balanceOf:          "70a08231",
  allowance:          "dd62ed3e",
  setApprovalForAll:  "a22cb465",
  isApprovedForAll:   "e985e9c5",
  getCollateral:      "5c1548fb",
  wrap:               "62355638", // wrap(address,address,uint256) — verified vs live onramp
  exactInputSingle:   "04e45aaf",
  quoteExactInputSingle: "c6a5026a",
};

export const MAX_UINT = (1n << 256n) - 1n;
export const APPROVE_TARGETS = [ADDR.EXCHANGE_V2, ADDR.NEGRISK_EXCHANGE_V2, ADDR.NEGRISK_ADAPTER];
export const CTF_OPERATORS = [ADDR.EXCHANGE_V2, ADDR.NEGRISK_EXCHANGE_V2];
export const FEE_TIERS = [100, 500, 3000];

// ---- encoding helpers (BigInt, no float) ----
export function addr32(a) {
  return a.toLowerCase().replace(/^0x/, "").padStart(64, "0");
}
export function uint32(n) {
  const v = BigInt(n);
  if (v < 0n) throw new Error("uint cannot be negative");
  return v.toString(16).padStart(64, "0");
}

export function parseUnits(amountStr, decimals = 6) {
  // exact decimal -> integer base units, FLOORS extra precision (no over-spend)
  const s = String(amountStr).trim();
  if (!/^\d*\.?\d*$/.test(s) || s === "" || s === ".") throw new Error("bad amount: " + amountStr);
  const [whole, frac = ""] = s.split(".");
  const fracPadded = (frac + "0".repeat(decimals)).slice(0, decimals);
  return BigInt((whole || "0") + fracPadded);
}
export function formatUnits(raw, decimals = 6) {
  const v = BigInt(raw);
  const neg = v < 0n;
  const a = (neg ? -v : v).toString().padStart(decimals + 1, "0");
  const whole = a.slice(0, a.length - decimals);
  const frac = a.slice(a.length - decimals).replace(/0+$/, "");
  return (neg ? "-" : "") + whole + (frac ? "." + frac : "");
}
export function minOut(amountIn, slippageBps) {
  const bps = BigInt(slippageBps);
  if (bps < 0n || bps > 10000n) throw new Error("slippage out of range");
  return (BigInt(amountIn) * (10000n - bps)) / 10000n;
}

// ---- calldata builders (return 0x-prefixed strings, identical to Python) ----
export const cdBalanceOf   = (owner) => "0x" + SEL.balanceOf + addr32(owner);
export const cdAllowance   = (owner, spender) => "0x" + SEL.allowance + addr32(owner) + addr32(spender);
export const cdApprove     = (spender, amount) => "0x" + SEL.approve + addr32(spender) + uint32(amount);
export const cdGetCollateral = () => "0x" + SEL.getCollateral;
export const cdIsApprovedForAll = (owner, op) => "0x" + SEL.isApprovedForAll + addr32(owner) + addr32(op);
export const cdSetApprovalForAll = (op, approved) => "0x" + SEL.setApprovalForAll + addr32(op) + uint32(approved ? 1 : 0);
export const cdWrap = (asset, to, amount) => "0x" + SEL.wrap + addr32(asset) + addr32(to) + uint32(amount);

export function cdExactInputSingle({ tokenIn, tokenOut, fee, recipient, amountIn, amountOutMin }) {
  return "0x" + SEL.exactInputSingle +
    addr32(tokenIn) + addr32(tokenOut) + uint32(fee) + addr32(recipient) +
    uint32(amountIn) + uint32(amountOutMin) + uint32(0); // sqrtPriceLimitX96 = 0
}
export function cdQuoteExactInputSingle({ tokenIn, tokenOut, amountIn, fee }) {
  return "0x" + SEL.quoteExactInputSingle +
    addr32(tokenIn) + addr32(tokenOut) + uint32(amountIn) + uint32(fee) + uint32(0);
}

// ---- pure planner (mirrors Python plan()) ----
export function planSteps(state, targetUnits) {
  const steps = [];
  const tgt = BigInt(targetUnits);
  if (BigInt(state.pusd) < tgt) {
    const need = tgt - BigInt(state.pusd);
    if (BigInt(state.usdce) < need) {
      const short = need - BigInt(state.usdce);
      steps.push({ step: "swap", detail: `native USDC → USDC.e for ${formatUnits(short)}` });
    }
    steps.push({ step: "wrap", detail: `USDC.e → pUSD for ${formatUnits(need)}` });
  }
  for (const ex of APPROVE_TARGETS) {
    if (BigInt(state.exchAllow?.[ex] ?? 0) < tgt) {
      steps.push({ step: "approve-pusd", spender: ex, detail: `approve pUSD → ${ex.slice(0, 10)}…` });
    }
  }
  for (const ex of CTF_OPERATORS) {
    if (!state.ctfOperator?.[ex]) {
      steps.push({ step: "approve-ctf", operator: ex, detail: `CTF approve → ${ex.slice(0, 10)}… (to sell)` });
    }
  }
  if (steps.length === 0) steps.push({ step: "ready", detail: "collateral is tradeable" });
  return steps;
}

// Concrete ordered MetaMask tx list to make `amountUnits` of pUSD tradeable.
// Pure: returns [{key, kind, to, data, detail}]; the UI sends each via eth_sendTransaction.
// `quote` = {fee, out} (USDC.e out for the swap leg) or null if no swap needed yet.
export function buildActions(state, amountUnits, quote = null, slippageBps = 50) {
  const amt = BigInt(amountUnits);
  const haveP = BigInt(state.pusd ?? 0), haveU = BigInt(state.usdce ?? 0);
  const needP = amt > haveP ? amt - haveP : 0n;          // pUSD shortfall
  const needU = needP > haveU ? needP - haveU : 0n;      // native USDC to swap
  const a = [];
  if (needU > 0n) {
    if (BigInt(state.routerAllowNative ?? 0) < needU)
      a.push({ key: "approve-router", kind: "approve", to: ADDR.NATIVE_USDC,
        data: cdApprove(ADDR.UNISWAP_ROUTER, MAX_UINT),
        detail: "Approve Uniswap router to spend native USDC" });
    if (quote)
      a.push({ key: "swap", kind: "swap", to: ADDR.UNISWAP_ROUTER,
        data: cdExactInputSingle({ tokenIn: ADDR.NATIVE_USDC, tokenOut: ADDR.USDCE, fee: quote.fee,
          recipient: state.funder, amountIn: needU, amountOutMin: minOut(quote.out, slippageBps) }),
        detail: `Swap ${formatUnits(needU)} native USDC → ~${formatUnits(quote.out)} USDC.e (min ${formatUnits(minOut(quote.out, slippageBps))})` });
  }
  if (needP > 0n) {
    if (BigInt(state.onrampAllowUsdce ?? 0) < needP)
      a.push({ key: "approve-onramp", kind: "approve", to: ADDR.USDCE,
        data: cdApprove(ADDR.ONRAMP, MAX_UINT),
        detail: "Approve CollateralOnramp to spend USDC.e" });
    a.push({ key: "wrap", kind: "wrap", to: ADDR.ONRAMP,
      data: cdWrap(ADDR.USDCE, state.funder, needP),
      detail: `Wrap ${formatUnits(needP)} USDC.e → pUSD` });
  }
  for (const ex of APPROVE_TARGETS)
    if (BigInt(state.exchAllow?.[ex] ?? 0) < amt)
      a.push({ key: "approve-pusd-" + ex, kind: "approve", to: ADDR.PUSD,
        data: cdApprove(ex, MAX_UINT), detail: `Approve pUSD → exchange ${ex.slice(0, 10)}…` });
  for (const ex of CTF_OPERATORS)
    if (!state.ctfOperator?.[ex])
      a.push({ key: "ctf-" + ex, kind: "approve", to: ADDR.CTF,
        data: cdSetApprovalForAll(ex, true), detail: `Enable selling: CTF operator ${ex.slice(0, 10)}…` });
  return a;
}

export function isRightNetwork(chainIdHexOrNum) {
  if (typeof chainIdHexOrNum === "string") return chainIdHexOrNum.toLowerCase() === CHAIN_HEX;
  return Number(chainIdHexOrNum) === CHAIN_ID;
}

export const POLYGON_ADD_PARAMS = {
  chainId: CHAIN_HEX,
  chainName: "Polygon Mainnet",
  nativeCurrency: { name: "POL", symbol: "POL", decimals: 18 },
  rpcUrls: ["https://polygon-rpc.com"],
  blockExplorerUrls: ["https://polygonscan.com"],
};
