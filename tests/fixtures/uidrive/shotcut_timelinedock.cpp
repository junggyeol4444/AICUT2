// Excerpt of shotcut/src/docks/timelinedock.cpp from the project's own source, kept so the learner is
// tested against the real thing without a network.

>start - 1,
                     Settings.timelineRipple());
        }
    });
    connect(this, &TimelineDock::selectionChanged, action, [=]() {
        auto selectedClips = selection();
        bool enabled = selectedClips.size() == 1;
        if (enabled) {
            enabled = !isBlank(selectedClips.first().y(), selectedClips.first().x())
                      && !isTransition(selectedClips.first().y(), selectedClips.first().x());
        }
        action->setEnabled(enabled);
    });
    Actions.add("timelineNudgeBackwardAction", action);

    action = new QAction(tr("Append"), this);
    action->setShortcut(QKeySequence(Qt::Key_A));
    icon = QIcon::fromTheme("list-add", QIcon(":/icons/oxygen/32x32/actions/list-add.png"));
    action->setIcon(icon);
    connect(action, &QAction::triggered, this, [&]() {
        show();
        raise();
        append(currentTrack());
    });
    Actions.add("timelineAppendAction", action);

    action = new QAction(tr("Ripple Delete"), this);
    QList<QKeySequence> deleteShortcuts;
    deleteShortcuts << QKeySequence(Qt::Key_X);
    deleteSh