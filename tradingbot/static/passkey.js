/* SolEntry passkeys (WebAuthn) — plain JavaScript, no dependencies.
 *
 * Register: Settings page button -> create() -> finish.
 * Sign in: Login page button -> get() -> finish -> dashboard.
 *
 * If the browser has no passkey support, the buttons say so plainly
 * instead of failing silently. Nothing secret is ever logged.
 */
(function () {
  "use strict";

  function b64urlToBuffer(s) {
    s = s.replace(/-/g, "+").replace(/_/g, "/");
    while (s.length % 4) s += "=";
    var bin = atob(s);
    var buf = new Uint8Array(bin.length);
    for (var i = 0; i < bin.length; i++) buf[i] = bin.charCodeAt(i);
    return buf.buffer;
  }

  function bufferToB64url(buf) {
    var bytes = new Uint8Array(buf);
    var bin = "";
    for (var i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
    return btoa(bin).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  }

  function decodeOptions(options) {
    options.challenge = b64urlToBuffer(options.challenge);
    if (options.user && options.user.id) {
      options.user.id = b64urlToBuffer(options.user.id);
    }
    (options.excludeCredentials || []).forEach(function (c) {
      c.id = b64urlToBuffer(c.id);
    });
    (options.allowCredentials || []).forEach(function (c) {
      c.id = b64urlToBuffer(c.id);
    });
    return options;
  }

  function csrfToken() {
    var el = document.querySelector('input[name="csrf_token"]');
    return el ? el.value : "";
  }

  function setStatus(text) {
    var el = document.getElementById("passkey-status");
    if (el) el.textContent = text;
  }

  function postJson(url, body) {
    return fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).then(function (resp) {
      return resp.json().then(function (data) {
        if (!resp.ok) throw new Error(data.error || "Request failed.");
        return data;
      });
    });
  }

  function finishCredential(url, credential, transports) {
    return postJson(url, {
      csrf_token: csrfToken(),
      credential: credential,
      transports: transports || [],
    });
  }

  function captureTransports(credential) {
    try {
      if (credential.response && credential.response.getTransports) {
        return credential.response.getTransports();
      }
    } catch (e) { /* not all responses expose transports */ }
    return [];
  }

  function encodeCredential(credential) {
    var out = {
      id: credential.id,
      rawId: bufferToB64url(credential.rawId),
      type: credential.type,
      response: {},
    };
    if (credential.response.clientDataJSON) {
      out.response.clientDataJSON = bufferToB64url(
        credential.response.clientDataJSON);
    }
    if (credential.response.attestationObject) {
      out.response.attestationObject = bufferToB64url(
        credential.response.attestationObject);
    }
    if (credential.response.authenticatorData) {
      out.response.authenticatorData = bufferToB64url(
        credential.response.authenticatorData);
    }
    if (credential.response.signature) {
      out.response.signature = bufferToB64url(credential.response.signature);
    }
    if (credential.response.userHandle) {
      out.response.userHandle = bufferToB64url(
        credential.response.userHandle);
    }
    return out;
  }

  function noSupport(btn) {
    if (btn) {
      btn.disabled = true;
      btn.textContent = "Passkeys are not supported in this browser.";
    }
    setStatus("Your browser does not support passkeys. Use your password instead.");
  }

  function initRegister() {
    var btn = document.getElementById("passkey-register-btn");
    if (!btn) return;
    if (!window.PublicKeyCredential) { noSupport(btn); return; }
    btn.addEventListener("click", function () {
      setStatus("Waiting for your device — approve the passkey prompt…");
      postJson("/settings/passkey/register/begin",
        { csrf_token: csrfToken() })
        .then(function (options) {
          return navigator.credentials.create({
            publicKey: decodeOptions(options),
          });
        })
        .then(function (credential) {
          return finishCredential("/settings/passkey/register/finish",
            encodeCredential(credential),
            captureTransports(credential));
        })
        .then(function (data) {
          if (data.ok) {
            setStatus("Passkey registered. You can now sign in with it.");
            window.location.reload();
          } else {
            setStatus(data.error || "Registration failed.");
          }
        })
        .catch(function (err) {
          setStatus(err.message || "Registration failed.");
        });
    });
  }

  function initLogin() {
    var btn = document.getElementById("passkey-login-btn");
    if (!btn) return;
    if (!window.PublicKeyCredential) { noSupport(btn); return; }
    btn.addEventListener("click", function () {
      var userEl = document.querySelector('input[name="username"]');
      var username = userEl ? userEl.value.trim() : "";
      if (!username) {
        setStatus("Type your username first, then tap “Sign in with passkey”.");
        return;
      }
      setStatus("Waiting for your device — approve the passkey prompt…");
      postJson("/login/passkey/begin",
        { csrf_token: csrfToken(), username: username })
        .then(function (options) {
          return navigator.credentials.get({
            publicKey: decodeOptions(options),
          });
        })
        .then(function (credential) {
          return finishCredential("/login/passkey/finish",
            encodeCredential(credential),
            captureTransports(credential));
        })
        .then(function (data) {
          if (data.ok) {
            window.location.href = data.redirect || "/dashboard";
          } else {
            setStatus(data.error || "Sign-in failed.");
          }
        })
        .catch(function (err) {
          setStatus(err.message || "Sign-in failed.");
        });
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    initRegister();
    initLogin();
  });
})();
