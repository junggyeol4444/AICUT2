// Excerpt of kdenlive/src/monitor/monitormanager.cpp from the project's own source, kept so the learner is
// tested against the real thing without a network.

 QStringLiteral("monitor"));

    QAction *zoneStart = new QAction(QIcon::fromTheme(QStringLiteral("media-seek-backward")), i18n("Go to Zone Start"), this);
    connect(zoneStart, &QAction::triggered, this, &MonitorManager::slotZoneStart);
    pCore->window()->addAction(QStringLiteral("seek_zone_start"), zoneStart, Qt::SHIFT | Qt::Key_I, QStringLiteral("navandplayback"));

    QAction *zoneEnd = new QAction(QIcon::fromTheme(QStringLiteral("media-seek-forward")), i18n("Go to Zone End"), this);
    connect(zoneEnd, &QAction::triggered, this, &MonitorManager::slotZoneEnd);
    pCore->window()->addAction(QStringLiteral("seek_zone_end"), zoneEnd, Qt::SHIFT | Qt::Key_O, QStringLiteral("navandplayback"));

    QAction *markIn = new QAction(QIcon::fromTheme(QStringLiteral("zone-in")), i18n("Set Zone In"), this);
    connect(markIn, &QAction::triggered, this, &MonitorManager::slotSetInPoint);
    pCore->window()->addAction(QStringLiteral("mark_in"), markIn, Qt::Key_I);

    QAction *markOut = new QAction(QIcon::fromTheme(QStringLiteral("zone-out")), i18n("Set Zone Out"), this);
    connect(markOut, &QAction::triggered, this, &MonitorManager::slotSetOutPoint);
    pCore->window()->addAction(QStringLiteral("mark_out"), markOut, Qt::Key_O);
}

void MonitorManager::refreshIcons()
{
    QList<QAction *> allMenus = this->findChildren<QAction *>();
    for (int i = 0; i < allMenus.count(); i++