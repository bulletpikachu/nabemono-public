const form = document.querySelector("#login-form");
const password = document.querySelector("#password");
const error = document.querySelector("#login-error");
const button = form.querySelector("button");

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  error.hidden = true;
  error.textContent = "";
  button.disabled = true;

  try {
    const response = await fetch("/login", {
      method: "POST",
      body: new URLSearchParams({password: password.value}),
    });
    if (response.ok) {
      window.location.assign("/");
      return;
    }
    error.textContent = response.status === 429
      ? "too many attempts..."
      : "incorrect...";
    error.hidden = false;
  } catch {
    error.textContent = "an error occurred";
    error.hidden = false;
  } finally {
    button.disabled = false;
  }
});
