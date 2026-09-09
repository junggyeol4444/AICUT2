/* Reading the Common Edit Model inside an editor (플러그인 기획안 37장, 38장).
 *
 * 37장 puts this structure between the AI Engine and any editor API:
 *
 *     AI Engine -> Common Edit Model -> Editor Adapter -> 편집기
 *
 * This is the JavaScript twin of `aicut_model.py`, for the editors that script
 * in JavaScript rather than Python. It answers the same questions in the same
 * words, so an adapter written against either reads one meaning of the model
 * and not two - which is the whole point of 37장 putting a layer here at all.
 *
 * Nothing in this file touches an editor, so it runs under plain node, which
 * is how it is tested on a machine with no Adobe install on it.
 *
 * ES3 on purpose: ExtendScript is not modern JavaScript. No let, no const, no
 * arrow functions, no JSON guarantee.
 */

var aicutModel = (function () {
    "use strict";

    function ModelError(message) {
        this.name = "ModelError";
        this.message = message;
    }
    ModelError.prototype = new Error();

    function fail(message) {
        throw new ModelError(message);
    }

    function parse(text, where) {
        var model;
        try {
            // ExtendScript has no JSON in older hosts, so eval is the fallback
            // every Adobe script uses. The input is a document aicut wrote.
            model = (typeof JSON !== "undefined" && JSON.parse)
                ? JSON.parse(text)
                : eval("(" + text + ")");
        } catch (e) {
            fail((where || "the model") + " is not valid JSON: " + e.message);
        }
        return validated(model, where);
    }

    /* Refuse a document that is not a Common Edit Model, saying which it is. */
    function validated(model, where) {
        var name = where || "this file";
        if (!model || typeof model !== "object") {
            fail("an edit model is a JSON object");
        }
        if (model.cuts && !model.sequences) {
            fail("this is an aicut edit plan, not a Common Edit Model. 37장 has "
                 + "the adapter read the model, so ask the running engine for it: "
                 + "start it with 'aicut ui' and fetch "
                 + "http://127.0.0.1:8765/api/episodes/<id>/edit-model.");
        }
        if (!model.sequences) {
            fail(name + " has no 'sequences'; it is not a Common Edit Model");
        }
        if (!model.sequences.length) {
            fail("this model has no sequence in it - there is no timeline to build");
        }
        return model;
    }

    /* Where the file for one media id is, as the model states it. */
    function mediaPath(model, mediaId) {
        var media = model.media || [];
        var i, path;
        for (i = 0; i < media.length; i++) {
            if (media[i].media_id === mediaId) {
                path = media[i].path || "";
                if (!path) { fail("media " + mediaId + " has no path"); }
                return path;
            }
        }
        fail("the model has no media called " + mediaId);
    }

    function sequences(model) {
        return (model.sequences || []).slice(0);
    }

    /* The sequence's tracks of one kind, in index order.
     *
     * 24장: 필요한 Track을 AI가 구성한다. There are as many as the model has and
     * no more, so an adapter creating a fixed set would add empty ones. */
    function tracks(sequence, kind) {
        var all = sequence.tracks || [];
        var out = [];
        var i;
        for (i = 0; i < all.length; i++) {
            if (all[i].kind === kind) { out.push(all[i]); }
        }
        out.sort(function (x, y) { return (x.index || 0) - (y.index || 0); });
        return out;
    }

    /* The parts of one clip that survive pacing, in source seconds.
     *
     * A clip carries the spans an editor must drop from inside it (9.3); a
     * timeline that ignores them plays the dead air the plan decided to remove.
     */
    function keptSpans(clip) {
        var spans = [[Number(clip.in_point_sec), Number(clip.out_point_sec)]];
        var removals = [];
        var raw = clip.remove_spans || [];
        var i, j, out, start, end, a, b;

        for (i = 0; i < raw.length; i++) {
            removals.push([Number(raw[i][0]), Number(raw[i][1])]);
        }
        removals.sort(function (x, y) { return x[0] - y[0]; });

        for (i = 0; i < removals.length; i++) {
            start = removals[i][0];
            end = removals[i][1];
            out = [];
            for (j = 0; j < spans.length; j++) {
                a = spans[j][0];
                b = spans[j][1];
                if (end <= a || start >= b) {
                    out.push([a, b]);
                    continue;
                }
                if (start > a) { out.push([a, Math.min(start, b)]); }
                if (end < b) { out.push([Math.max(end, a), b]); }
            }
            spans = out;
        }

        out = [];
        for (i = 0; i < spans.length; i++) {
            if (spans[i][1] - spans[i][0] > 1e-3) { out.push(spans[i]); }
        }
        return out;
    }

    /* Every clip of the main video track, in timeline order.
     *
     * Timeline order, not source order: 2.4 lets a video open on a moment that
     * happened last, and sorting by in-point would quietly rebuild the
     * broadcast. */
    function videoClips(sequence) {
        var video = tracks(sequence, "video");
        var clips;
        if (!video.length) { fail("this sequence has no video track"); }
        clips = (video[0].clips || []).slice(0);
        clips.sort(function (x, y) {
            return (x.timeline_position_sec || 0) - (y.timeline_position_sec || 0);
        });
        return clips;
    }

    /* Every surviving span as frames, in timeline order.
     *
     * The frame rate is the editor's, not the model's: a sequence built at a
     * different rate slides every cut, and the sequence the adapter is filling
     * is the one that decides.
     *
     * `endFrameInclusive` is the editor's own convention and has to be stated
     * by the adapter: Resolve's endFrame is the last frame of the clip,
     * Premiere's out point is the first frame after it. One frame either way on
     * every cut is not visible until the export. */
    function frameRanges(sequence, fps, endFrameInclusive) {
        var ranges = [];
        var clips, spans, i, j, startFrame, endFrame;
        if (!(fps > 0)) { fail("frame rate must be positive, got " + fps); }
        clips = videoClips(sequence);
        for (i = 0; i < clips.length; i++) {
            spans = keptSpans(clips[i]);
            for (j = 0; j < spans.length; j++) {
                startFrame = Math.round(spans[j][0] * fps);
                endFrame = Math.round(spans[j][1] * fps);
                if (endFrameInclusive) { endFrame -= 1; }
                if (endFrameInclusive ? (endFrame < startFrame) : (endFrame <= startFrame)) {
                    // Shorter than one frame at this rate. Reported by
                    // droppedSpans rather than silently shifting what follows.
                    continue;
                }
                ranges.push({
                    startFrame: startFrame,
                    endFrame: endFrame,
                    frames: endFrameInclusive
                        ? (endFrame - startFrame + 1)
                        : (endFrame - startFrame),
                    clip: clips[i]
                });
            }
        }
        if (!ranges.length) {
            fail("every clip in this sequence is shorter than one frame at " + fps + " fps");
        }
        return ranges;
    }

    /* Spans too short to survive at this frame rate, so the caller can say so. */
    function droppedSpans(sequence, fps) {
        var dropped = [];
        var clips = videoClips(sequence);
        var i, j, spans;
        for (i = 0; i < clips.length; i++) {
            spans = keptSpans(clips[i]);
            for (j = 0; j < spans.length; j++) {
                if (Math.round(spans[j][1] * fps) <= Math.round(spans[j][0] * fps)) {
                    dropped.push(spans[j]);
                }
            }
        }
        return dropped;
    }

    /* A name a person can find again, not a bare id.
     *
     * 25장's default is a new sequence beside what the operator built, so the
     * name is what tells the two apart in their bin. */
    function timelineName(model, sequence) {
        var stated = String(sequence.name || "");
        stated = stated.replace(/^\s+|\s+$/g, "");
        if (stated) { return stated; }
        return "AI_" + String(sequence.sequence_id || "sequence").substring(0, 8);
    }

    /* Every caption, in time order, from whatever subtitle tracks exist. */
    function subtitles(sequence) {
        var found = tracks(sequence, "subtitle");
        var lines = [];
        var i, j, rows;
        for (i = 0; i < found.length; i++) {
            rows = found[i].subtitles || [];
            for (j = 0; j < rows.length; j++) { lines.push(rows[j]); }
        }
        lines.sort(function (x, y) {
            return (x.start_sec || 0) - (y.start_sec || 0);
        });
        return lines;
    }

    /* Sound the model places itself - BGM and effects - with its track name.
     *
     * The video clips' own audio is not here: 24장 gives 원본 음성 its own track
     * and both Resolve and Premiere place a linked A/V clip, so an adapter that
     * also laid these down would double the source audio. */
    function audioClips(sequence) {
        var found = tracks(sequence, "audio");
        var placed = [];
        var i, j, rows;
        for (i = 0; i < found.length; i++) {
            rows = found[i].audio || [];
            for (j = 0; j < rows.length; j++) {
                placed.push({track: found[i].name || "", clip: rows[j]});
            }
        }
        return placed;
    }

    function baseName(path) {
        var text = String(path).replace(/\\/g, "/");
        var parts = text.split("/");
        return parts[parts.length - 1] || text;
    }

    /* One paragraph a person can check the timeline against. */
    function summary(model, sequence, fps, endFrameInclusive) {
        var ranges = frameRanges(sequence, fps, endFrameInclusive);
        var frames = 0;
        var source = (model.media && model.media.length)
            ? baseName(mediaPath(model, "source")) : "?";
        var i;
        for (i = 0; i < ranges.length; i++) { frames += ranges[i].frames; }
        return ranges.length + " clips, " + (frames / fps).toFixed(1) + "s at "
            + fps + " fps, from " + source;
    }

    return {
        ModelError: ModelError,
        parse: parse,
        validated: validated,
        mediaPath: mediaPath,
        sequences: sequences,
        tracks: tracks,
        keptSpans: keptSpans,
        videoClips: videoClips,
        frameRanges: frameRanges,
        droppedSpans: droppedSpans,
        timelineName: timelineName,
        subtitles: subtitles,
        audioClips: audioClips,
        baseName: baseName,
        summary: summary
    };
}());

// node, for the tests. ExtendScript has no module and must not see this.
if (typeof module !== "undefined" && module.exports) { module.exports = aicutModel; }
