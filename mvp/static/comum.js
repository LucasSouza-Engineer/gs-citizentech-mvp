const NOMES = ["normal", "atenção", "alerta", "alerta máximo"];
const $ = id => document.getElementById(id);
function esc(t) { const d = document.createElement("div"); d.textContent = t == null ? "" : String(t); return d.innerHTML; }
function selo(n) { n = Number(n) || 0; return `<span class="nivel n${n}">${NOMES[n]}</span>`; }
function cep(c) { c = String(c || "").replace(/\D/g, ""); return c.length === 8 ? c.slice(0, 5) + "-" + c.slice(5) : c; }
function hora(iso) {
  if (!iso) return "–";
  const d = new Date(iso.endsWith("Z") || iso.includes("+") ? iso : iso + "Z");
  return d.toLocaleString("pt-BR", { timeZone: "America/Sao_Paulo", day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
}
function num(v, casas = 0, suf = "") { return v == null ? "–" : Number(v).toFixed(casas).replace(".", ",") + suf; }

function blocoRisco(r) {
  return `<div class="risco">
    <div class="n${r.nivel_enchente}"><h2>Enchente</h2>${selo(r.nivel_enchente)}<p>${esc(r.motivo_enchente)}</p>
      <p class="selo">Chuva: ${num(r.chuva_1h_mm, 0, " mm")} em 1 h · ${num(r.chuva_24h_mm, 0, " mm")} em 24 h
      (${r.fonte_chuva === "cemaden" ? "pluviômetro do Cemaden a até 5 km" : "estação do INMET mais próxima"})</p></div>
    <div class="n${r.nivel_calor}"><h2>Calor</h2>${selo(r.nivel_calor)}<p>${esc(r.motivo_calor)}</p>
      <p class="selo">Agora: ${num(r.temp_atual_c, 1, " °C")} · máxima 24 h ${num(r.temp_max_24h_c, 1, " °C")}
      · sensação ${num(r.indice_calor_max_24h_c, 0, " °C")}</p></div>
  </div>`;
}
