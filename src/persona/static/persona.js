const form = document.querySelector("#fact-form");
const factId = document.querySelector("#fact-id");
const factKey = document.querySelector("#fact-key");
const factValue = document.querySelector("#fact-value");
const factAliases = document.querySelector("#fact-aliases");
const editorHeading = document.querySelector("#editor-heading");
const cancelEdit = document.querySelector("#cancel-edit");
const factList = document.querySelector("#fact-list");
const empty = document.querySelector("#empty");
const notice = document.querySelector("#notice");
const search = document.querySelector("#search");

let facts = [];
let csrfToken = "";

function showNotice(message, isError = false) {
  notice.textContent = message;
  notice.classList.toggle("error", isError);
}

async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  if (options.body && !(options.body instanceof FormData)) {
    headers.set("Content-Type", "application/json");
  }
  if (options.method && options.method !== "GET") {
    headers.set("X-CSRF-Token", csrfToken);
  }
  const response = await fetch(path, {...options, headers});
  if (response.status === 401) {
    window.location.assign("/login");
    throw new Error("Authentication required.");
  }
  if (!response.ok) {
    throw new Error((await response.text()) || `Request failed (${response.status}).`);
  }
  return response.json();
}

function resetForm() {
  form.reset();
  factId.value = "";
  editorHeading.textContent = "add a fact";
  cancelEdit.hidden = true;
}

function startEdit(fact) {
  factId.value = String(fact.id);
  factKey.value = fact.key;
  factValue.value = fact.value;
  factAliases.value = fact.aliases.join(", ");
  editorHeading.textContent = "edit fact";
  cancelEdit.hidden = false;
  factKey.focus();
  window.scrollTo({top: 0, behavior: "smooth"});
}

function makeButton(label, className, handler) {
  const button = document.createElement("button");
  button.type = "button";
  button.textContent = label;
  if (className) button.className = className;
  button.addEventListener("click", handler);
  return button;
}

function render() {
  const query = search.value.trim().toLowerCase();
  const visible = facts.filter((fact) => {
    const haystack = [fact.key, fact.value, ...fact.aliases].join(" ").toLowerCase();
    return !query || haystack.includes(query);
  });

  factList.replaceChildren();
  empty.hidden = visible.length !== 0;
  empty.textContent = facts.length ? "No matching persona facts." : "No persona facts yet.";

  for (const fact of visible) {
    const article = document.createElement("article");
    article.className = "fact";

    const heading = document.createElement("div");
    heading.className = "fact-heading";
    const title = document.createElement("h3");
    title.textContent = fact.key;
    heading.append(title);

    const actions = document.createElement("div");
    actions.className = "fact-actions";
    actions.append(
      makeButton("edit", "secondary", () => startEdit(fact)),
      makeButton("delete", "danger", async () => {
        if (!window.confirm(`Delete "${fact.key}"?`)) return;
        try {
          await api(`/api/persona/${fact.id}`, {method: "DELETE"});
          facts = facts.filter((item) => item.id !== fact.id);
          if (factId.value === String(fact.id)) resetForm();
          render();
          showNotice("Fact deleted.");
        } catch (error) {
          showNotice(error.message, true);
        }
      }),
    );
    heading.append(actions);
    article.append(heading);

    const value = document.createElement("p");
    value.className = "fact-value";
    value.textContent = fact.value;
    article.append(value);

    if (fact.aliases.length) {
      const aliases = document.createElement("p");
      aliases.className = "aliases";
      aliases.textContent = `aliases: ${fact.aliases.join(", ")}`;
      article.append(aliases);
    }
    factList.append(article);
  }
}

async function loadFacts() {
  try {
    const payload = await api("/api/persona");
    facts = payload.facts;
    csrfToken = payload.csrf_token;
    render();
  } catch (error) {
    showNotice(error.message, true);
  }
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const aliases = [...new Set(
    factAliases.value.split(",").map((item) => item.trim()).filter(Boolean),
  )];
  const payload = JSON.stringify({
    key: factKey.value,
    value: factValue.value,
    aliases,
  });
  const id = factId.value;
  try {
    const saved = await api(
      id ? `/api/persona/${id}` : "/api/persona",
      {method: id ? "PUT" : "POST", body: payload},
    );
    const index = facts.findIndex((fact) => fact.id === saved.id);
    if (index === -1) facts.push(saved);
    else facts[index] = saved;
    facts.sort((left, right) => left.key.localeCompare(right.key));
    resetForm();
    render();
    showNotice(id ? "Fact updated." : "Fact added.");
  } catch (error) {
    showNotice(error.message, true);
  }
});

cancelEdit.addEventListener("click", resetForm);
search.addEventListener("input", render);
document.querySelector("#logout").addEventListener("click", async () => {
  try {
    await api("/logout", {method: "POST"});
    window.location.assign("/login");
  } catch (error) {
    showNotice(error.message, true);
  }
});

loadFacts();
