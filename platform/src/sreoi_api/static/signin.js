// Posts the sign-in form as JSON so the shared /auth/login endpoint serves both
// the browser and API clients. Nothing is templated into this file: the target
// is read from a data attribute on <body>, so no server value reaches script.
(function () {
  var form = document.getElementById("signin");
  if (!form) return;
  var err = document.getElementById("err");

  function show(message) {
    err.textContent = message;
    err.hidden = false;
  }

  form.addEventListener("submit", function (event) {
    event.preventDefault();
    err.hidden = true;
    fetch("/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "same-origin",
      body: JSON.stringify({
        email: document.getElementById("email").value,
        password: document.getElementById("password").value
      })
    })
      .then(function (response) {
        if (response.ok) {
          // The response set an httponly cookie; the destination came from the
          // server already validated as a same-origin path.
          window.location.assign(document.body.dataset.next || "/");
          return null;
        }
        return response.json().then(function (body) {
          show((body && body.detail) || "Sign-in failed.");
        });
      })
      .catch(function () {
        show("Sign-in failed: the server could not be reached.");
      });
  });
})();
