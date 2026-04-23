from django.urls import path
from . import views, backtest_view
from . import replay_views


urlpatterns = [
    path('', views.option_chain_dashboard, name='dashboard'),
    # Admin panel और API endpoints
    path('admin-panel/', views.admin_panel_view,  name='admin_panel'),
    path('api/admin-status/', views.admin_status_api, name='admin_status_api'),
    path('api/update-bot-settings/', views.update_bot_settings_api, name='update_bot_settings_api'),
    path('api/close-all-trades/', views.close_all_open_trades_api, name='close_all_open_trades_api'),

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

    path('reversal-chart/', views.reversal_chart_view, name='reversal_chart'),


    path('option-chart/', views.option_chart_view, name='option_chart'),
    path("option-chart-api/", views.option_chart_api, name="option_chart_api"),

    path('api/resistance/', views.resistance_live_api, name='resistance_live_api'),
    path('resistance/', views.resistance_dashboard, name='resistance_dashboard'),
    path('sr-data/', views.support_resistance_view, name='sr_data'),

    # ── Market Replay ────────────────────────────────────────
    path('replay/', replay_views.market_replay_view, name='market_replay'),

    # AJAX endpoints
    path('api/replay/dates/',      replay_views.get_replay_dates,      name='api_replay_dates'),
    path('api/replay/timestamps/', replay_views.get_replay_timestamps,  name='api_replay_timestamps'),
    path('api/replay/tick/',       replay_views.get_replay_tick,        name='api_replay_tick'),
    path('api/replay/bulk/',       replay_views.get_replay_bulk, name='api_replay_bulk'),

    path("chart/",         views.chart_view,   name="chart"),        # मुख्य chart page
    path("api/candle/",    views.candle_api,    name="candle_api"),   # AJAX JSON data
    path("api/symbols/",   views.symbol_search, name="symbol_search"),# Autocomplete

    path('dashboard-chart/', views.dashboard_chart_view, name='dashboard_chart'),

    # बैकटेस्ट के लिए URL:
    path('backtesta/', views.run_backtest_view, name='run_backtest'),

    path('backtestc/', views.backtest_view, name='run_backtest'),

    path('backtest/', backtest_view.backtest_view, name='backtest'),

    # लाइव पेपर ट्रेड्स देखने के लिए नया URL:
    path('live-trades/', views.live_trades_view, name='live_trades'),
    path('api/dashboard-data/', views.dashboard_data_api, name='dashboard_data_api'),
    # 👇 यह Skip Trade URL जोड़ें
    path('api/skip-trade/', views.skip_trade_api, name='skip_trade_api'),
    # 👇 यह Add Manual Trade URL जोड़ें
    path('api/add-manual/', views.add_manual_trade_api, name='add_manual_trade'),

    # पुराने ट्रैड डेसबोर्ड के url 
    path('trade-journal/', views.trade_dashboard, name='trade_journal'),
    
]