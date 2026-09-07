const button = document.getElementById("analyze");
const status = document.getElementById("status");

button.onclick = () => {
  window.__adobe_cep__.evalScript("aicutSelectedMediaPath()", raw => {
    const selected = JSON.parse(raw || "{}");
    if (selected.error) { status.textContent = selected.error; return; }
    const child = require("child_process");
    const path = require("path");
    const extensionRoot = window.__adobe_cep__.getSystemPath("extension");
    const bridge = process.env.AICUT_ROOT
      ? path.resolve(process.env.AICUT_ROOT, "editor_plugins", "run_bridge.py")
      : path.resolve(extensionRoot, "..", "run_bridge.py");
    const python = process.env.AICUT_PYTHON || "python3";
    status.textContent = "Analyzing…";
    child.execFile(python, [bridge, "--media", selected.path], {maxBuffer: 16 * 1024 * 1024}, (error, stdout, stderr) => {
      status.textContent = error ? (stderr || error.message) : `Complete\n${stdout}`;
    });
  });
};
