// Acrescenta à tela de cadastro (componente de Visão, sem alterá-lo) a navegação do MVP
// e, depois do cadastro, o link para a página com o risco atual do endereço.
(function () {
  const nav = document.createElement("nav");
  nav.setAttribute("aria-label", "Navegação");
  nav.innerHTML = '<a href="/">Cadastro</a> · <a href="/painel">Painel de risco</a>';
  nav.style.cssText = "max-width:640px;margin:0 auto;padding:12px 16px 0;font-size:.95rem";
  document.body.prepend(nav);

  const fetchOriginal = window.fetch;
  window.fetch = async function (url, opts) {
    const r = await fetchOriginal.apply(this, arguments);
    if (String(url).endsWith("/api/v1/cadastros") && opts && opts.method === "POST" && r.ok) {
      r.clone().json().then(j => setTimeout(() => {
        const alvo = document.getElementById("status2");
        const p = document.createElement("p");
        p.innerHTML = `<a href="/meu?id=${encodeURIComponent(j.id)}" style="font-weight:700">Ver agora o risco no meu endereço →</a>`;
        alvo.appendChild(p);
        try { localStorage.setItem("cadastro_id", j.id); } catch (e) {}
      }, 50));
    }
    return r;
  };
})();
