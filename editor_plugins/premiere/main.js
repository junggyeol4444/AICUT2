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
    const workspace = process.env.AICUT_EDITOR_WORKSPACE || require("os").homedir() + "/.aicut-editor";
    const jobId = `${Date.now()}-${Math.random().toString(16).slice(2)}`;
    const resultFile = path.resolve(workspace, "jobs", `${jobId}.json`);
    const args = [bridge, "--media", selected.path, "--workspace", workspace, "--result-file", resultFile, "--job-id", jobId];
    if (process.env.AICUT_EDITOR_OPTIONS) args.push("--options-json", process.env.AICUT_EDITOR_OPTIONS);
    if (process.env.AICUT_EDITOR_MANIFEST) args.push("--manifest", process.env.AICUT_EDITOR_MANIFEST);
    status.textContent = "Analyzing…";
    child.execFile(python, args, {maxBuffer: 1024 * 1024}, (error, stdout, stderr) => {
      status.textContent = error ? (stderr || error.message) : `Complete\nResult: ${resultFile}`;
    });
  });
};
