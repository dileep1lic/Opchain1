from django.urls import path
from . import views, seed_voice
from . import replay_views




urlpatterns = [
    path('', views.option_chain_dashboard, name='dashboard'),
    # Admin panel और API endpoints
    path('admin-panel/', views.admin_panel_view,  name='admin_panel'),
    path('api/admin-status/', views.admin_status_api, name='admin_status_api'),
    path('api/update-bot-settings/', views.update_bot_settings_api, name='update_bot_settings_api'),
    path('api/close-all-trades/', views.close_all_open_trades_api, name='close_all_open_trades_api'),
    path('api/db-cleanup/',       views.db_cleanup_api,            name='db_cleanup_api'),
    path('api/db-cleanup-preview/', views.db_cleanup_preview_api,  name='db_cleanup_preview_api'),
    
    path('admin-panel/users/', views.user_approval_list, name='user_approval_list'),
    path('admin-panel/users/toggle/<int:user_id>/', views.toggle_user_status, name='toggle_user_status'),

    # यह लाइन जोड़ें ताकि Django हमारे कस्टम लॉगिन व्यू को कॉल करे
    path('accounts/login/', views.login_view, name='login'), 
    
    # रजिस्टर का URL (अगर आपने पहले जोड़ लिया है तो ठीक है)
    path('register/', views.register_user, name='register_user'),

    # लूप्स को चालू/बंद करने वाला URL (जैसे: /toggle/nifty_loop/)
    path('toggle/<str:loop_name>/', views.toggle_sync, name='toggle_sync'),
    path('table-update-url/', views.table_update_api, name='table_update_api'),
    path('stock-dashboard/', views.all_stocks_dashboard, name='stock_dashboard'),
    path('search-dashboard/', views.stock_search_view, name='search_dashboard'),
    path('update-expiries/', views.trigger_expiry_update, name='update_expiries'),

    path('chart/view/oi/', views.render_chart_page, name='chart_page_oi'), # HTML पेज
    path('api/oi-data/', views.specific_strike_oi_data, name='oi_data_api'), # JSON डेटा
    # COI
    path('chart/view/coi/', views.render_chart_page_coi, name='chart_page_coi'), # HTML पेज
    path('api/coi-data/', views.specific_strike_coi_data, name='coi_data_api'), # JSON डेटा
    # LTP
    path('chart/view/ltp/', views.render_chart_page_ltp, name='chart_page_ltp'), # HTML पेज
    path('api/ltp-data/', views.specific_strike_ltp_data, name='ltp_data_api'), # JSON डेटा

    # path('reversal-chart/', views.reversal_chart_view, name='reversal_chart'),


    path('api/resistance/', views.resistance_live_api, name='resistance_live_api'),
    path('resistance/', views.resistance_dashboard, name='resistance_dashboard'),
    path('sr-data/', views.support_resistance_view, name='sr_data'),

    # ── Market Replay ────────────────────────────────────────
    # path('replay/', replay_views.market_replay_view, name='market_replay'),
    # test code 
    path('market-replay/', views.market_replay_view, name='market_replay'),
    path('api/market-replay-data/', views.market_replay_data_api, name='market_replay_data_api'),

    # AJAX endpoints
    path('api/replay/dates/',      replay_views.get_replay_dates,      name='api_replay_dates'),
    path('api/replay/timestamps/', replay_views.get_replay_timestamps,  name='api_replay_timestamps'),
    path('api/replay/tick/',       replay_views.get_replay_tick,        name='api_replay_tick'),
    path('api/replay/bulk/',       replay_views.get_replay_bulk, name='api_replay_bulk'),

    path("chart/",         views.chart_view,   name="chart"),        # मुख्य chart page
    path("api/candle/",    views.candle_api,    name="candle_api"),   # AJAX JSON data
    path("api/symbols/",   views.symbol_search, name="symbol_search"),# Autocomplete

    path('dashboard-chart/', views.dashboard_chart_view, name='dashboard_chart'),

    # लाइव पेपर ट्रेड्स देखने के लिए नया URL:
    path('live-trades/', views.live_trades_view, name='live_trades'),
    path('api/dashboard-data/', views.dashboard_data_api, name='dashboard_data_api'),

    # 👇 यह Skip Trade URL जोड़ें
    path('api/skip-trade/', views.skip_trade_api, name='skip_trade_api'),
    # 👇 यह Add Manual Trade URL जोड़ें
    path('api/add-manual/', views.add_manual_trade_api, name='add_manual_trade'),
    # पुराने ट्रैड डेसबोर्ड के url 
    path('trade-journal/', views.trade_dashboard, name='trade_journal'),

    # बैकटेस्ट Trade के लिए URL:
    path('backtesta/', views.backtest_view, name='backtest'),
    path('api/backtest/run/', views.backtest_run_api, name='backtest_run'),

    # Trade journal backtest ricord के लिए URLs
    path('journal/',                    views.journal_list,   name='journal_list'),
    path('journal/add/',                views.journal_create, name='journal_create'),
    path('journal/edit/<int:pk>/',      views.journal_edit,   name='journal_edit'),
    path('journal/delete/<int:pk>/',    views.journal_delete, name='journal_delete'),



    # ── Voice Command Player ──────────────────────────────
    path('monika', seed_voice.index, name='calling'),
    path('api/voice-chat/', seed_voice.voice_chat_api, name='voice_chat_api'),
    path('api/voice-chat/stream/', seed_voice.voice_chat_stream, name='voice_chat_stream'),  # 🚀 SSE Streaming

    
]