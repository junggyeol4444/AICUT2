/* The Premiere Pro half of the arithmetic: frames and ticks (37장, 38장).
 *
 *     AI Engine -> Common Edit Model -> Premiere Adapter -> Premiere Pro
 *
 * `plugin/common/aicut_model.js` answers what the model means; this file turns
 * that answer into the numbers Premiere's own API wants, and nothing here
 * touches Premiere, so it runs under plain node - which is how it is tested on
 * a machine with no Adobe install on it. `aicut_premiere.jsx` is the part that
 * talks to the application.
 *
 * ES3 on purpose: ExtendScript is not modern JavaScript.
 */

/*global aicutModel */

var aicutBuild = (function (model) {
    "use strict";

    // Premiere counts time in ticks, not seconds or frames. This number is
    // fixed in the application and is what every Time object converts through.
    var TICKS_PER_SECOND = 254016000000;

    // Premiere's out point is the first frame NOT included: a clip from frame
    // 0 to frame 30 at 30 fps is one second long. Resolve's endFrame is the
    // opposite, which is exactly why both adapters state it out loud instead of
    // leaving it in the arithmetic.
    var OUT_POINT_IS_EXCLUSIVE = true;

    function ticks(seconds) {
        // A string, because ExtendScript's Number loses precision well before
        // 2.5e11 ticks a second does, and Premiere's Time.ticks is a string too.
        return String(Math.round(seconds * TICKS_PER_SECOND));
    }

    /* What the script inserts, in timeline order.
     *
     * Timeline order is the model's, not source order: 2.4 lets a video open on
     * the moment that happened last, and sorting by source would quietly undo
     * the structure the engine chose.
     *
     * Every span is snapped to the sequence's own frame grid before it becomes
     * ticks. Inserting at raw seconds leaves sub-frame gaps that accumulate
     * into a drift no one can find by the end of a long timeline. */
    function clipList(sequence, fps) {
        var ranges = model.frameRanges(sequence, fps, !OUT_POINT_IS_EXCLUSIVE);
        var entries = [];
        var timeline = 0;
        var i, inSec, outSec;
        for (i = 0; i < ranges.length; i++) {
            inSec = ranges[i].startFrame / fps;
            outSec = ranges[i].endFrame / fps;
            entries.push({
                inSeconds: inSec,
                outSeconds: outSec,
                timelineSeconds: timeline,
                inTicks: ticks(inSec),
                outTicks: ticks(outSec),
                timelineTicks: ticks(timeline),
                frames: ranges[i].frames,
                name: ranges[i].clip.name || "",
                role: ranges[i].clip.role || ""
            });
            timeline += ranges[i].frames / fps;
        }
        return entries;
    }

    function summary(document, sequence, fps) {
        return model.summary(document, sequence, fps, !OUT_POINT_IS_EXCLUSIVE);
    }

    return {
        TICKS_PER_SECOND: TICKS_PER_SECOND,
        OUT_POINT_IS_EXCLUSIVE: OUT_POINT_IS_EXCLUSIVE,
        ticks: ticks,
        clipList: clipList,
        summary: summary
    };
}(typeof aicutModel !== "undefined"
    ? aicutModel
    : require("../common/aicut_model.js")));

if (typeof module !== "undefined" && module.exports) { module.exports = aicutBuild; }
