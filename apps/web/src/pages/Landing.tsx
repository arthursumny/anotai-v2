import { useEffect } from "react";
import { Link } from "react-router-dom";
import Brand from "../components/Brand";
import Icon from "../components/Icon";

function useReveal() {
  useEffect(() => {
    const els = Array.from(document.querySelectorAll(".landing-reveal"));
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches || !("IntersectionObserver" in window)) {
      els.forEach((el) => el.classList.add("visible"));
      return;
    }
    const io = new IntersectionObserver((entries) => {
      for (const entry of entries) {
        if (entry.isIntersecting) {
          entry.target.classList.add("visible");
          io.unobserve(entry.target);
        }
      }
    }, { threshold: 0.12 });
    els.forEach((el) => io.observe(el));
    return () => io.disconnect();
  }, []);
}

const benefits = ["Encontre suas ideias depois", "Lembre o que precisa ser feito", "Acompanhe cada próximo passo"];

export default function Landing() {
  useReveal();

  return (
    <div className="landing">
      <nav className="landing-nav" aria-label="Navegação principal">
        <div className="landing-nav-inner">
          <Link to="/" className="landing-brand" aria-label="AnotAI, página inicial"><Brand /></Link>
          <div className="landing-links">
            <a href="#como-ajuda">Como ajuda</a>
            <a href="#recursos">O que você pode fazer</a>
            <a href="#exemplos">Exemplos</a>
          </div>
          <div className="landing-actions">
            <Link to="/login" className="btn btn-secondary">Entrar</Link>
            <Link to="/login?modo=cadastro" className="btn">Criar conta</Link>
          </div>
        </div>
      </nav>

      <main>
        <header className="landing-hero">
          <div className="landing-copy">
            <span className="landing-eyebrow"><span /> Um jeito mais leve de organizar o dia</span>
            <h1>Tire as ideias da cabeça. <span>Leve o dia com mais clareza.</span></h1>
            <p className="landing-lead">
              Registre uma ideia ou compromisso falando. Encontre suas notas depois e acompanhe o que ainda precisa de atenção, tudo no mesmo lugar.
            </p>
            <div className="landing-cta">
              <Link to="/login?modo=cadastro" className="btn btn-lg">Começar agora <Icon name="arrowRight" /></Link>
              <a href="#como-ajuda" className="btn btn-lg btn-secondary">Ver como funciona</a>
            </div>
            <ul className="landing-benefits" aria-label="Benefícios">
              {benefits.map((benefit) => <li key={benefit}><Icon name="check" size={16} />{benefit}</li>)}
            </ul>
          </div>

          <div className="landing-preview-wrap" aria-label="Exemplo de notas e tarefas organizadas">
            <div className="landing-preview">
              <div className="preview-topbar">
                <span className="preview-brand"><span className="preview-brand-mark"><Icon name="mic" size={15} /></span> Seu espaço</span>
                <span className="preview-account">A</span>
              </div>
              <div className="preview-content">
                <div className="preview-heading">
                <div><span className="preview-kicker">EXEMPLO DE VISÃO GERAL</span><h2>Um pouco mais em ordem</h2></div>
                  <span className="preview-label">Exemplo</span>
                </div>
                <div className="preview-summary">
                  <div><span className="preview-summary-icon"><Icon name="note" size={17} /></span><span><b>Notas guardadas</b><small>Ideias e informações fáceis de encontrar</small></span></div>
                  <div><span className="preview-summary-icon"><Icon name="board" size={17} /></span><span><b>Próximos passos</b><small>O que você decidiu acompanhar</small></span></div>
                </div>
                <div className="preview-columns">
                  <section className="preview-panel">
                    <div className="preview-panel-title"><b>Notas recentes</b><span>Ver todas</span></div>
                    <div className="preview-note"><span className="preview-note-mark" /><div><b>Ideias para o projeto</b><small>Rever as referências e escolher uma direção</small><time>Hoje</time></div></div>
                    <div className="preview-note"><span className="preview-note-mark muted-mark" /><div><b>Conversa com a equipe</b><small>Decisões e pontos para retomar</small><time>Ontem</time></div></div>
                  </section>
                  <section className="preview-panel preview-tasks">
                    <div className="preview-panel-title"><b>Para acompanhar</b><span>2 itens</span></div>
                    <div className="preview-task"><span className="task-check" /><span>Enviar a proposta revisada</span></div>
                    <div className="preview-task"><span className="task-check" /><span>Marcar retorno com a equipe</span></div>
                    <div className="preview-task done-task"><span className="task-check"><Icon name="check" size={12} /></span><span>Separar ideias da reunião</span></div>
                  </section>
                </div>
                <div className="preview-foot"><span className="preview-status-dot" /> Um lugar para registrar, encontrar e continuar</div>
              </div>
            </div>
            <div className="preview-note-float"><span><Icon name="check" size={17} /></span><div><b>Ficou registrado</b><small>Volte quando precisar</small></div></div>
          </div>
        </header>

        <section id="como-ajuda" className="landing-section">
          <div className="landing-section-head landing-reveal">
            <span className="landing-section-kicker">COMO AJUDA</span>
            <h2>Menos esforço para lembrar.<br /><span>Mais facilidade para continuar.</span></h2>
            <p>Uma ideia surge no meio do dia. Registre agora, encontre depois e decida o próximo passo no seu tempo.</p>
          </div>
          <div className="landing-steps">
            <article className="landing-step landing-reveal">
              <span className="landing-step-number">01</span>
              <div className="landing-step-icon"><Icon name="mic" size={21} /></div>
              <h3>Registre do seu jeito</h3>
              <p>Fale uma ideia, um compromisso ou algo que não quer deixar passar. Não precisa parar para escrever.</p>
            </article>
            <article className="landing-step landing-reveal">
              <span className="landing-step-number">02</span>
              <div className="landing-step-icon"><Icon name="note" size={21} /></div>
              <h3>Encontre quando precisar</h3>
              <p>Suas gravações viram notas que você pode consultar e buscar pelo assunto ou por um trecho.</p>
            </article>
            <article className="landing-step landing-reveal">
              <span className="landing-step-number">03</span>
              <div className="landing-step-icon"><Icon name="board" size={21} /></div>
              <h3>Continue de onde parou</h3>
              <p>Transforme sugestões em tarefas, acompanhe prazos e veja o que já foi concluído.</p>
            </article>
          </div>
        </section>

        <section id="recursos" className="landing-section landing-section-soft">
          <div className="landing-section-head landing-reveal">
            <span className="landing-section-kicker">NO DIA A DIA</span>
            <h2>Seu trabalho continua.<br /><span>As pequenas coisas não se perdem.</span></h2>
            <p>Organize o que costuma ficar espalhado entre conversas, lembretes e anotações rápidas.</p>
          </div>
          <div className="landing-feature-grid">
            {[
              { icon: "mic" as const, title: "Anote sem interromper", text: "Guarde uma ideia ou informação enquanto ela ainda está fresca, sem precisar digitar tudo." },
              { icon: "search" as const, title: "Volte ao que importa", text: "Pesquise suas notas e encontre aquele detalhe quando chegar a hora de usar." },
              { icon: "board" as const, title: "Saiba o que vem a seguir", text: "Reúna tarefas em um quadro simples, mova cada item e acompanhe o andamento." },
              { icon: "grid" as const, title: "Repita rotinas com facilidade", text: "Use modelos para organizar registros que seguem um formato parecido." },
            ].map((feature) => (
              <article className="landing-feature landing-reveal" key={feature.title}>
                <span className="landing-feature-icon"><Icon name={feature.icon} size={19} /></span>
                <h3>{feature.title}</h3>
                <p>{feature.text}</p>
              </article>
            ))}
          </div>
        </section>

        <section id="exemplos" className="landing-section">
          <div className="landing-section-head landing-reveal">
            <span className="landing-section-kicker">EXEMPLOS</span>
            <h2>Para as coisas que acontecem <span>entre uma tarefa e outra.</span></h2>
            <p>Use a voz para registrar o que está acontecendo na sua rotina, sem precisar preparar uma anotação perfeita.</p>
          </div>
          <div className="landing-example-grid">
            <article className="landing-example landing-reveal"><span>01 / DEPOIS DE UMA CONVERSA</span><h3>Guarde decisões e combinações</h3><p>Registre os pontos importantes e volte a eles quando for preparar o próximo passo.</p></article>
            <article className="landing-example landing-reveal"><span>02 / NO MEIO DA ROTINA</span><h3>Não perca uma ideia rápida</h3><p>Deixe a informação registrada sem trocar de contexto para abrir um documento.</p></article>
            <article className="landing-example landing-reveal"><span>03 / NA HORA DE AGIR</span><h3>Transforme intenção em tarefa</h3><p>Coloque o próximo passo no quadro e acompanhe até concluir.</p></article>
          </div>
        </section>

        <section className="landing-bottom-cta landing-reveal">
          <span className="landing-section-kicker">COMECE COM A PRÓXIMA IDEIA</span>
          <h2>Abra espaço na cabeça<br /><span>para o que vem agora.</span></h2>
          <p>Crie sua conta e experimente um jeito mais simples de manter suas ideias e tarefas por perto.</p>
          <Link to="/login?modo=cadastro" className="btn btn-lg">Criar minha conta <Icon name="arrowRight" /></Link>
        </section>
      </main>

      <footer className="landing-footer"><Brand compact tagline={false} /><span>Ideias em ordem, no seu ritmo.</span><Link to="/login">Entrar na sua conta</Link></footer>
    </div>
  );
}
