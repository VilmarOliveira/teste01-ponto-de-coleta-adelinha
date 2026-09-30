document.querySelectorAll('form[method="post"], form[method="POST"]').forEach((form) => {
  if (!form.querySelector('[name="csrf_token"]')) {
    const input = document.createElement('input');
    input.type = 'hidden';
    input.name = 'csrf_token';
    input.value = document.querySelector('meta[name="csrf-token"]')?.content || '';
    form.appendChild(input);
  }
});

document.querySelectorAll('[data-signature-form]').forEach((form) => {
  const canvas = form.querySelector('canvas.signature');
  const hidden = form.querySelector('input[name="signature"], input.signature-value');
  const ctx = canvas.getContext('2d');
  let drawing = false;
  let moved = false;
  const point = (event) => {
    const bounds = canvas.getBoundingClientRect();
    const pointer = event.touches?.[0] || event;
    return {
      x: (pointer.clientX - bounds.left) * canvas.width / bounds.width,
      y: (pointer.clientY - bounds.top) * canvas.height / bounds.height,
    };
  };
  const start = (event) => {
    drawing = true;
    const position = point(event);
    ctx.beginPath();
    ctx.moveTo(position.x, position.y);
    event.preventDefault();
  };
  const move = (event) => {
    if (!drawing) return;
    const position = point(event);
    ctx.lineWidth = 3;
    ctx.lineCap = 'round';
    ctx.strokeStyle = '#282620';
    ctx.lineTo(position.x, position.y);
    ctx.stroke();
    moved = true;
    event.preventDefault();
  };
  const stop = () => { drawing = false; };
  ['mousedown', 'touchstart'].forEach((name) => canvas.addEventListener(name, start, { passive: false }));
  ['mousemove', 'touchmove'].forEach((name) => canvas.addEventListener(name, move, { passive: false }));
  ['mouseup', 'mouseleave', 'touchend'].forEach((name) => canvas.addEventListener(name, stop));
  form.querySelector('.clear-signature')?.addEventListener('click', () => {
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    moved = false;
  });
  form.addEventListener('submit', (event) => {
    if (!moved) {
      event.preventDefault();
      alert('Faça sua assinatura antes de continuar.');
      return;
    }
    hidden.value = canvas.toDataURL('image/png');
  });
});

document.querySelectorAll('[data-copy]').forEach((button) => button.addEventListener('click', async () => {
  const target = document.querySelector(button.dataset.copy);
  await navigator.clipboard.writeText(target.innerText);
  const previous = button.innerText;
  button.innerText = 'Copiado!';
  setTimeout(() => { button.innerText = previous; }, 1500);
}));

const clientId = document.querySelector('#client-id');
const clientLabel = document.querySelector('#client-label');
const clientResults = document.querySelector('#package-client-results');
const clientSearch = document.querySelector('#package-client-search');

function selectClient(id, name, publicId) {
  if (!clientId || !clientLabel) return;
  clientId.value = id;
  clientLabel.value = `${name} · ${publicId}`;
  clientLabel.classList.add('selected-client');
  if (clientResults) clientResults.replaceChildren();
}

// Os resultados da busca superior continuam podendo preencher o formulário.
document.querySelectorAll('[data-client-id]').forEach((button) => button.addEventListener('click', () => {
  const [name, publicId] = button.dataset.clientLabel.split(' · ');
  selectClient(button.dataset.clientId, name, publicId);
  clientLabel?.scrollIntoView({ behavior: 'smooth', block: 'center' });
}));

if (clientSearch && clientResults) {
  let requestNumber = 0;
  let timer;
  clientSearch.addEventListener('input', () => {
    clearTimeout(timer);
    // Alterar a pesquisa invalida uma seleção anterior para evitar vínculo acidental.
    clientId.value = '';
    clientLabel.value = '';
    clientLabel.classList.remove('selected-client');
    const query = clientSearch.value.trim();
    clientResults.replaceChildren();
    if (!query) return;
    const currentRequest = ++requestNumber;
    timer = setTimeout(async () => {
      clientResults.textContent = 'Buscando…';
      try {
        const response = await fetch(`/painel/clientes/busca?q=${encodeURIComponent(query)}`, {
          headers: { Accept: 'application/json' },
        });
        if (!response.ok) throw new Error('Falha na busca');
        const data = await response.json();
        if (currentRequest !== requestNumber) return;
        clientResults.replaceChildren();
        if (!data.clients.length) {
          clientResults.textContent = 'Nenhum cliente encontrado.';
          return;
        }
        data.clients.forEach((client) => {
          const button = document.createElement('button');
          button.type = 'button';
          button.className = 'client-result';
          const name = document.createElement('b');
          name.textContent = client.name;
          const details = document.createElement('span');
          details.textContent = `${client.public_id} · ${client.phone}`;
          button.append(name, details);
          button.addEventListener('click', () => selectClient(client.id, client.name, client.public_id));
          clientResults.appendChild(button);
        });
      } catch (_error) {
        clientResults.textContent = 'Não foi possível buscar. Tente novamente.';
      }
    }, 250);
  });
}

document.querySelector('#package-form')?.addEventListener('submit', (event) => {
  if (!clientId.value) {
    event.preventDefault();
    alert('Busque e selecione um cliente válido antes de registrar o pacote.');
    clientSearch.focus();
  }
});
