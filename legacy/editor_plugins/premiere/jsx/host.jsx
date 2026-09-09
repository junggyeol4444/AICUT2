function aicutSelectedMediaPath() {
    var project = app.project;
    var selection = project.getSelection();
    if (!selection || selection.length !== 1 || !selection[0].getMediaPath) {
        return JSON.stringify({error: "Select exactly one imported video clip"});
    }
    return JSON.stringify({path: selection[0].getMediaPath()});
}
