// Excerpt of shotcut/src/player.cpp from the project's own source, kept so the learner is
// tested against the real thing without a network.

t(QKeySequence(Qt::CTRL | Qt::Key_J));
    connect(action, &QAction::triggered, this, [&]() {
        DurationDialog dialog(this);
        dialog.setDuration(qRound(MLT.profile().fps() * Settings.playerJumpSeconds()));
        if (dialog.exec() == QDialog::Accepted) {
            Settings.setPlayerJumpSeconds((double) dialog.duration() / MLT.profile().fps());
        }
    });
    Actions.add("playerSetJumpAction", action);

    action = new QAction(tr("Trim Clip In"), this);
    action->setShortcut(QKeySequence(Qt::Key_I));
    connect(action, &QAction::triggered, this, [&]() {
        if (tabIndex() == Player::SourceTabIndex && MLT.isSeekableClip()) {
            setIn(position());
            int delta = position() - MLT.producer()->get_in();
            emit inChanged(delta);
        } else if (tabIndex() == Player::ProjectTabIndex) {
            emit trimIn();
        }
    });
    Actions.add("playerSetInAction", action);

    action = new QAction(tr("Trim Clip Out"), this);
    action->setShortcut(QKeySequence(Qt::Key_O));
    connect(action, &QAction::triggered, this, [&]() {
t::Key_I));
    connect(action, &QAction::triggered, this, [&]() {
        if (tabIndex() == Player::SourceTabIndex && MLT.isSeekableClip()) {
            setIn(position());
            int delta = position() - MLT.producer()->get_in();
            emit inChanged(delta);
        } else if (tabIndex() == Player::ProjectTabIndex) {
            emit trimIn();
        }
    });
    Actions.add("playerSetInAction", action);

    action = new QAction(tr("Trim Clip Out"), this);
    action->setShortcut(QKeySequence(Qt::Key_O));
    connect(action, &QAction::triggered, this, [&]() {
        if (tabIndex() == Player::SourceTabIndex && MLT.isSeekableClip()) {
            setOut(position());
            int delta = position() - MLT.producer()->get_out();
            emit outChanged(delta);
        } else if (tabIndex() == Player::ProjectTabIndex) {
            emit trimOut();
        }
    });
    Actions.add("playerSetOutAction", action);

    action = new QAction(tr("Set Time Position"), this);
    action->setShortcut(QKeySequence(Qt::CTRL | Qt::Key_T));
    connect(action, &QAction::triggere