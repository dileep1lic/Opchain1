from django.urls import path
from . import views

urlpatterns = [
    path('', views.option_chain_dashboard, name='dashboard'),
    # लूप्स को चालू/बंद करने वाला URL (जैसे: /toggle/nifty_loop/)
    path('toggle/<str:loop_name>/', views.toggle_sync, name='toggle_sync'),
    path('table-update-url/', views.table_update_api, name='table_update_api'),
    path('stock-dashboard/', views.all_stocks_dashboard, name='stock_dashboard'),
    path('search-dashboard/', views.stock_search_view, name='search_dashboard'),
    path('update-expiries/', views.trigger_expiry_update, name='update_expiries'),
]