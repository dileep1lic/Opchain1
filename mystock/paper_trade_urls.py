# अपने app के urls.py में यह add करें:
# path('paper/', include('yourapp.paper_trade_urls')),

from django.urls import path
from . import paper_trade_views

urlpatterns = [
    # Main dashboard page
    path('dashboard/', paper_trade_views.paper_trade_dashboard, name='paper_trade_dashboard'),
    
    # API Endpoints (frontend इन्हें हर 5 सेकंड में call करेगा)
    path('api/dashboard-data/', paper_trade_views.dashboard_data, name='dashboard_data'),
    path('api/check-enter/', paper_trade_views.check_and_enter_trade, name='check_and_enter'),
    path('api/monitor/', paper_trade_views.monitor_open_trades, name='monitor_trades'),
]
