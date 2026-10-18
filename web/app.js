// app.js — MetaMask front-end for the Polymarket V2 funnel.
// All signing happens in MetaMask; this page never sees a private key.
import * as f from "./funnel.js";

const $ = (id) => document.getElementById(id);
const eth = () => window.ethereum;
let account = null;
let amountUnits = f.parseUnits("5");

function log(msg, cls = "") { const el = $("log"); el.innerHTML = `<div class="${cls}">${msg}</div>` + el.innerHTML; }
const link = (h) => `<a href="https://polygonscan.com/tx/${h}" target="_blank">${h.slice(0, 14)}…</a>`;

async function rpc(method, params) { return eth().request({ method, params }); }
async function call(to, data) {
  const r = await rpc("eth_call", [{ to, data }, "latest"]);
  return r && r !== "0x" ? BigInt(r) : 0n;
}

async function connect() {
  if (!eth()) { log("MetaMask not found — install it.", "bad"); return; }
  const accts = await rpc("eth_requestAccounts", []);
  account = accts[0];
  $("acct").textContent = account;
  eth().on?.("accountsChanged", () => location.reload());
  eth().on?.("chainChanged", () => location.reload());
  await checkNetwork();
  await refresh();
}

async function checkNetwork() {
  const cid = await rpc("eth_chainId", []);
  const ok = f.isRightNetwork(cid);
  $("net").textContent = ok ? "Polygon ✓" : `WRONG NETWORK (${cid}) — must be Polygon`;
  $("net").className = ok ? "ok" : "bad";
  $("switchBtn").style.display = ok ? "none" : "inline-block";
  return ok;
}

async function switchNetwork() {
  try {
    await rpc("wallet_switchEthereumChain", [{ chainId: f.CHAIN_HEX }]);
  } catch (e) {
    if (e.code === 4902) await rpc("wallet_addEthereumChain", [f.POLYGON_ADD_PARAMS]);
  }
  await checkNetwork(); await refresh();
}

async function readState() {
  const owner = account;
  const bal = (t) => call(t, f.cdBalanceOf(owner));
  const allow = (t, s) => call(t, f.cdAllowance(owner, s));
  const op = async (o) => (await call(f.ADDR.CTF, f.cdIsApprovedForAll(owner, o))) === 1n;
  const exchAllow = {};
  for (const ex of f.APPROVE_TARGETS) exchAllow[ex] = await allow(f.ADDR.PUSD, ex);
  const ctfOperator = {};
  for (const ex of f.CTF_OPERATORS) ctfOperator[ex] = await op(ex);
  const matic = BigInt(await rpc("eth_getBalance", [owner, "latest"]));
  return {
    funder: owner,
    native: await bal(f.ADDR.NATIVE_USDC), usdce: await bal(f.ADDR.USDCE), pusd: await bal(f.ADDR.PUSD),
    matic,
    routerAllowNative: await allow(f.ADDR.NATIVE_USDC, f.ADDR.UNISWAP_ROUTER),
    onrampAllowUsdce: await allow(f.ADDR.USDCE, f.ADDR.ONRAMP),
    exchAllow, ctfOperator,
  };
}

async function quoteSwap(amountIn) {
  let best = null;
  for (const fee of f.FEE_TIERS) {
    const data = f.cdQuoteExactInputSingle({ tokenIn: f.ADDR.NATIVE_USDC, tokenOut: f.ADDR.USDCE, amountIn, fee });
    try {
      const r = await rpc("eth_call", [{ to: f.ADDR.UNISWAP_QUOTER, data }, "latest"]);
      if (r && r !== "0x") {
        const out = BigInt("0x" + r.slice(2, 66));
        if (!best || out > best.out) best = { fee, out };
      }
    } catch { /* tier has no route */ }
  }
  return best;
}

async function refresh() {
  if (!account) return;
  if (!(await checkNetwork())) return;
  log("reading chain…");
  const s = await readState();
  $("bNative").textContent = "$" + f.formatUnits(s.native);
  $("bUsdce").textContent = "$" + f.formatUnits(s.usdce);
  $("bPusd").textContent = "$" + f.formatUnits(s.pusd);
  $("bMatic").textContent = (Number(s.matic) / 1e18).toFixed(4) + " POL";
  $("gasWarn").style.display = s.matic === 0n ? "block" : "none";

  const needP = amountUnits > s.pusd ? amountUnits - s.pusd : 0n;
  const needU = needP > s.usdce ? needP - s.usdce : 0n;
  const quote = needU > 0n ? await quoteSwap(needU) : null;
  if (quote) $("quote").textContent =
    `Swap quote: ${f.formatUnits(needU)} native USDC → ${f.formatUnits(quote.out)} USDC.e @ ${quote.fee / 10000}%`;
  else $("quote").textContent = needU > 0n ? "no swap route found" : "no swap needed";

  const actions = f.buildActions(s, amountUnits, quote);
  const box = $("actions"); box.innerHTML = "";
  if (actions.length === 0) {
    box.innerHTML = `<div class="ok big">✓ Ready — pUSD collateral is in place. Polymarket can see it.</div>`;
    return;
  }
  actions.forEach((a, i) => {
    const row = document.createElement("div"); row.className = "action";
    row.innerHTML = `<span><b>${i + 1}.</b> ${a.detail}<br><small>to ${a.to}</small></span>`;
    const btn = document.createElement("button");
    btn.textContent = i === 0 ? "Send via MetaMask" : "…";
    btn.disabled = i !== 0;             // enforce order: one at a time, top-down
    btn.onclick = () => sendAction(a);
    row.appendChild(btn); box.appendChild(row);
  });
}

async function sendAction(a) {
  if (!(await checkNetwork())) { alert("Switch to Polygon first."); return; }
  const human = `Confirm on Polygon:\n\n${a.detail}\n\nContract: ${a.to}\nFrom: ${account}\n\nMetaMask will show the exact transaction next.`;
  if (!confirm(human)) return;
  try {
    const hash = await rpc("eth_sendTransaction", [{ from: account, to: a.to, data: a.data, value: "0x0" }]);
    log(`sent ${a.key}: ${link(hash)}`, "ok");
    // simple poll for receipt
    const poll = setInterval(async () => {
      const rc = await rpc("eth_getTransactionReceipt", [hash]).catch(() => null);
      if (rc) { clearInterval(poll); log(`confirmed ${a.key} (status ${rc.status})`, rc.status === "0x1" ? "ok" : "bad"); refresh(); }
    }, 3000);
  } catch (e) { log(`failed ${a.key}: ${e.message}`, "bad"); }
}

$("connectBtn").onclick = connect;
$("switchBtn").onclick = switchNetwork;
$("refreshBtn").onclick = refresh;
$("amount").oninput = (e) => {
  try { amountUnits = f.parseUnits(e.target.value || "0"); $("amtErr").textContent = ""; refresh(); }
  catch { $("amtErr").textContent = "invalid amount"; }
};
