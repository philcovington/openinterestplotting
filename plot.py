import schwab
import configparser
import httpx
import argparse
import pandas as pd
import pandas_ta as ta
import matplotlib.pyplot as plt
import datetime as dt
from matplotlib.animation import FuncAnimation
import matplotlib
import numpy as np
import json

from datetime import timedelta

matplotlib.rcParams['toolbar'] = 'None'

_ticker = 'tqqq'.upper()
expiry_date_string = '2024-07-30'
span = 20.0
bar_width = 0.3
update_rate = 5000
show_yesterday = False

# Read configurations from config file
config = configparser.ConfigParser()
config.read('schwab_config.ini')

api_key = config['schwab']['api_key']
app_secret = config['schwab']['app_secret']
redirect_uri = config['schwab']['redirect_uri']
token_path = config['schwab']['token_path']
account_number = config['schwab']['account_number']

open_interest_df = None
schwab_client = None
yesterday_ohlc = {}

def get_the_next_friday(given_date):
    # If the given date is a Friday, return it
    if given_date.weekday() == 4:  # Friday is represented by 4
        return given_date.strftime('%Y-%m-%d')
    
    # Calculate the number of days until the next Friday
    days_until_friday = (4 - given_date.weekday() + 7) % 7
    next_friday_date = given_date + timedelta(days=days_until_friday)
    
    # Return the next Friday date as a string in the format %Y-%m-%d
    return next_friday_date.strftime('%Y-%m-%d')

def initialize_schwab_client(reauthorize: bool=False):
    """Initialize Schwab client and handle authentication."""
    try:
        if not reauthorize:
            return schwab.auth.client_from_token_file(token_path, api_key, app_secret)
        else:
            return schwab.auth.client_from_manual_flow(api_key, app_secret, redirect_uri, token_path)
    except FileExistsError:
        return schwab.auth.client_from_manual_flow(api_key, app_secret, redirect_uri, token_path)
    except FileNotFoundError:
        return schwab.auth.client_from_manual_flow(api_key, app_secret, redirect_uri, token_path)

def get_account_hash(schwab_client):
    """Retrieve account hash for the given account number."""
    try:
        resp = schwab_client.get_account_numbers()
    except schwab.auth.OAuth2Client.oauth_error_class as e:
        print(e)
        schwab_client = schwab.auth.client_from_manual_flow(api_key, app_secret, redirect_uri, token_path)
        resp = retry_get_account_numbers(schwab_client)
    
    if resp.status_code in {httpx.codes.OK, httpx.codes.CREATED}:
        return extract_account_hash(resp)
    else:
        print(resp.status_code)
        exit(-1)

def retry_get_account_numbers(schwab_client):
    """Retry getting account numbers after re-authentication."""
    try:
        return schwab_client.get_account_numbers()
    except schwab.auth.OAuth2Client.oauth_error_class as e:
        print(e)
        exit(-1)

def extract_account_hash(resp):
    """Extract account hash from the response."""
    resp_json = resp.json()
    for id in resp_json:
        if id['accountNumber'] == str(account_number):
            account_hash = id['hashValue']
            print(f'Account hash: {account_hash}')
            return account_hash
    print("Account number not found.")
    exit(-1)

def calculate_adr(df: pd.DataFrame, days: int=5):
    adr = ta.sma(df['high'] - df['low'], length=days)
    return round(adr.iloc[-1], 2)

def get_ohlc_history(days: int=5) -> pd.DataFrame:
    global schwab_client, _ticker

    start_date = dt.date.today() - dt.timedelta(days=days*2)
    end_date = dt.date.today() - dt.timedelta(days=1)
    start_datetime = dt.datetime.combine(start_date, dt.time())
    end_datetime = dt.datetime.combine(end_date, dt.time())
    resp = schwab_client.get_price_history_every_day(_ticker, start_datetime=start_datetime, end_datetime=end_datetime)  
    if resp.status_code in {httpx.codes.OK, httpx.codes.CREATED}:
        data = resp.json()        
        candles = data.get('candles')
        if len(candles) < days:
            print("Not enough data!")
            return None
               
        df = pd.DataFrame(candles)
        
        # Convert datetime column from milliseconds to seconds
        df['datetime'] = df['datetime'] / 1000

        # Convert datetime column to datetime object
        df['datetime'] = pd.to_datetime(df['datetime'], unit='s')

        # Convert datetime column to string date
        df['date_string'] = df['datetime'].dt.strftime('%Y-%m-%d %H:%M:%S')

        # Set the date_string column as the index
        df.set_index('date_string', inplace=True)

        # Drop the original datetime column if not needed
        df.drop(columns=['datetime'], inplace=True)

        return(df)        
       
    else:
        print("Failed to fetch historical data")
        return None
    
def get_yesterdays_ohlc() -> dict:
    global schwab_client, _ticker
    OHLC_data = {}
    yesterday_date = dt.date.today() - dt.timedelta(days=1)
    yesterday_datetime = dt.datetime.combine(yesterday_date, dt.time())
    resp = schwab_client.get_price_history_every_day(_ticker, start_datetime=yesterday_datetime, end_datetime=yesterday_datetime)  
    if resp.status_code in {httpx.codes.OK, httpx.codes.CREATED}:
        data = resp.json()
        print(data)
        open_price = data.get('candles')[0].get('open')
        high_price = data.get('candles')[0].get('high')
        low_price = data.get('candles')[0].get('low')
        close_price = data.get('candles')[0].get('close') 
        OHLC_data = {'open': open_price, 'high': high_price, 'low': low_price, 'close': close_price}     
        return OHLC_data  
    else:
        print("Failed to fetch historical data")
        return {}
    
def calculate_open_interest(data_map, option_type):
    open_interest = {}
    for exp_date, strikes in data_map.items():
        for strike_price, options in strikes.items():
            option_info = options[0]  # Assuming each list contains one option data dict
            strike = float(option_info['strikePrice'])
            oi = option_info['openInterest']
            if strike not in open_interest:
                open_interest[strike] = {'CALL': 0, 'PUT': 0}
            open_interest[strike][option_type] += oi
    return open_interest

# Calculate total open interest by summing call and put open interest for each strike price
def calculate_total_open_interest(call_open_interest, put_open_interest):
    total_open_interest = {}

    # Iterate over call open interest data
    for strike, data in call_open_interest.items():
        total_open_interest[strike] = data['CALL'] + put_open_interest.get(strike, {}).get('PUT', 0)

    # Add any strikes that are only in put open interest data
    for strike, data in put_open_interest.items():
        if strike not in total_open_interest:
            total_open_interest[strike] = data['PUT']

    return total_open_interest

def calculate_max_pain(open_interest_df):
    max_pain = 0
    min_total_loss = float('inf')
    
    for strike in open_interest_df['Strike']:
        total_loss = 0
        for _, row in open_interest_df.iterrows():
            if row['Strike'] <= strike:
                # For calls, loss is max(0, strike price - option's strike price)
                total_loss += row['Calls'] * max(0, strike - row['Strike'])
            else:
                # For puts, loss is max(0, option's strike price - strike price)
                total_loss += row['Puts'] * max(0, row['Strike'] - strike)
        
        if total_loss < min_total_loss:
            min_total_loss = total_loss
            max_pain = strike
    
    return max_pain

def update_data(from_date: dt.date, to_date: dt.date, lower_strike: float=0.0, upper_strike: float=10000.0):
    global open_interest_df
    put_call_ratio = 0
    total_calls = 0
    total_puts = 0
    max_call_interest = 0
    max_interest_value = 0
    max_put_interest = 0 
    last_price = 0.0
    high_price = 0.0
    low_price = 0.0 
    open_price = 0.0
    call_data = None
    put_data = None

    resp = schwab_client.get_option_chain(_ticker, contract_type=schwab.client.Client.Options.ContractType.ALL, strategy=schwab.client.Client.Options.Strategy.ANALYTICAL, from_date=from_date, to_date=to_date, include_underlying_quote=False)
    if resp.status_code in {httpx.codes.OK, httpx.codes.CREATED}:
        data = resp.json()        
        call_data = data.get('callExpDateMap', {})
        put_data = data.get('putExpDateMap', {})
        call_open_interest = calculate_open_interest(call_data, 'CALL')
        put_open_interest = calculate_open_interest(put_data, 'PUT')
        
        # total_open_interest = calculate_total_open_interest(call_open_interest, put_open_interest)
        open_interest_data = []

        # Calculate total open interest for calls and puts
        total_calls = 0
        total_puts = 0
        max_call_interest = 0
        max_put_interest = 0       

        # Iterate over call open interest data for total calls and max call interest
        for strike, oi in call_open_interest.items():
            call_oi = oi['CALL']
            total_calls += call_oi  # Sum total calls across all strike prices
            if lower_strike <= strike <= upper_strike:
                if call_oi > max_call_interest:
                    max_call_interest = call_oi

        # Iterate over put open interest data for total puts and max put interest
        for strike, oi in put_open_interest.items():
            put_oi = oi['PUT']
            total_puts += put_oi  # Sum total puts across all strike prices
            if lower_strike <= strike <= upper_strike:
                if put_oi > max_put_interest:
                    max_put_interest = put_oi   

        max_interest_value = max(max_call_interest, max_put_interest)

        put_call_ratio = total_puts / total_calls if total_calls != 0 else float('inf')

        for strike in set(call_open_interest.keys()).union(set(put_open_interest.keys())):
            calls_oi = call_open_interest.get(strike, {}).get('CALL', 0)
            puts_oi = put_open_interest.get(strike, {}).get('PUT', 0)
            open_interest_data.append({'Strike': strike, 'Calls': calls_oi, 'Puts': puts_oi})

        open_interest_df = pd.DataFrame(open_interest_data)         
        
    resp = schwab_client.get_quote(_ticker)
    if resp.status_code in {httpx.codes.OK, httpx.codes.CREATED}:
        data = resp.json()
        quote = data.get(_ticker).get('quote')
        open_price = round(quote.get('openPrice'), 2)
        last_price = round(quote.get('lastPrice'), 2)
        high_price = round(quote.get('highPrice'), 2)
        low_price = round(quote.get('lowPrice'), 2)

    return open_interest_df, open_price, last_price, high_price, low_price, put_call_ratio, max_interest_value       

def on_click(event):
    global open_interest_df
    # Check if the right mouse button (usually button 1) was clicked
    if event.button == 1:
        # Check if the click was inside the plot area
        if event.inaxes == ax:
            # Calculate the nearest x-axis index (strike price) to the clicked point
            x_clicked = event.xdata
            nearest_strike = min(open_interest_df['Strike'], key=lambda x: abs(x - x_clicked))
            # Get the maximum open interest for the nearest strike
            call_oi = open_interest_df.loc[open_interest_df['Strike'] == nearest_strike, 'Calls'].values[0]
            put_oi = open_interest_df.loc[open_interest_df['Strike'] == nearest_strike, 'Puts'].values[0]
            max_oi = max(call_oi, put_oi)
            # Update the position and text of the annotation
            annotation.xy = (nearest_strike, max_oi)
            text = f"Strike: {nearest_strike:.2f}\nOI: {max_oi}"
            annotation.set_text(text)
            annotation.set_visible(True)
            fig.canvas.draw_idle()
        else:
            annotation.set_visible(False)
        fig.canvas.draw_idle()
    elif event.button == 3:
        # Check if the click was inside the plot area
        if event.inaxes == ax:
            # Toggle the visibility of the annotation
            if annotation.get_visible():
                annotation.set_visible(False)
                fig.canvas.draw_idle()

def update(frame):
    open_interest_df, open_price, last_price, high_price, low_price, put_call_ratio, _ = update_data(from_date=from_date_obj, to_date=to_date_obj, lower_strike=graph_min, upper_strike=graph_max)
    ax.clear()
    ax.bar(open_interest_df['Strike'], open_interest_df['Calls'], label='Calls', alpha=0.7, color='blue', width=bar_width)
    ax.bar(open_interest_df['Strike'], open_interest_df['Puts'], label='Puts', alpha=0.7, color='red', width=bar_width)
    ax.axvline(x=last_price, color='green', linestyle='--', label=f'Last Price')
    ax.axvline(x=high_price, color='magenta', linestyle='-', label=f'High Price')
    ax.axvline(x=low_price, color='cyan', linestyle='-', label=f'Low Price')

    # Calculate max pain
    max_pain = calculate_max_pain(open_interest_df)

    ylow = yesterday_ohlc.get('low')
    yhi = yesterday_ohlc.get('high')

    if ylow is not None and yhi is not None and show_yesterday:
        if last_price > ylow and last_price < yhi:
            # Plot yesterday's high and low as horizontal lines
            if yesterday_ohlc is not None:
                ax.axvline(x=yesterday_ohlc.get('open'), color='purple', linestyle='dashdot', label='Yesterday\'s Open')
                ax.axvline(x=yhi, color='green', linestyle='dashdot', label='Yesterday\'s High')
                ax.axvline(x=ylow, color='red', linestyle='dashdot', label='Yesterday\'s Low')
                ax.axvline(x=yesterday_ohlc.get('close'), color='blue', linestyle='dashdot', label='Yesterday\'s Close')

    # Add vertical lines for open_price - adr5 and open_price + adr5
    open_price_minus_adr5 = open_price - adr5
    open_price_plus_adr5 = open_price + adr5
    ax.axvline(x=open_price_minus_adr5, color='orange', linestyle='--', label=f'ADR DN')
    ax.axvline(x=open_price_plus_adr5, color='purple', linestyle='--', label=f'ADR UP')

    ax.axvline(x=max_pain, color='gray', linestyle='dashed', label='Max pain', linewidth=4)

    ax.axvspan(0, low_price, color='darkgray', alpha=0.5)
    ax.axvspan(high_price, 1e6, color='darkgray', alpha=0.5)

    ax.set_xlabel('Strike Price')
    ax.set_ylabel('Open Interest')
    ax.set_title(f'Open Interest by Strike Price for {_ticker.upper()}')
    ax.legend()
    ax.grid(True)

    # Set the y-axis limits
    ax.set_ylim(0, max_interest+max_interest*0.1)

    # Set the x-axis limits
    ax.set_xlim(graph_min, graph_max)  # Replace min_x and max_x with your desired values

    # Annotate the last price
    ax.text(last_price, ax.get_ylim()[1] * 0.95, f'${last_price:.2f}', color='green',
            ha='center', va='bottom', backgroundcolor='white')

    # Add the Expiry Date annotation below the x-axis
    ax.text(0.05, -0.1, f'Expiry: {to_date_obj}', transform=ax.transAxes,
            fontsize=12, color='black', ha='left', va='top', backgroundcolor='lightgrey')

    ax.text(0.25, -0.1, f'Max Pain: ${max_pain:.2f}', transform=ax.transAxes,
            fontsize=12, color='black', ha='left', va='top', backgroundcolor='lightgrey')

    # Add the Put/Call Ratio annotation below the x-axis
    ax.text(0.45, -0.1, f'Put/Call Ratio: {put_call_ratio:.2f}', transform=ax.transAxes,
            fontsize=12, color='black', ha='left', va='top', backgroundcolor='lightgrey')

    ax.text(0.65, -0.1, f'Range: ${high_price-low_price:.2f}', transform=ax.transAxes,
            fontsize=12, color='black', ha='left', va='top', backgroundcolor='lightgrey')

    # Add the "Last Updated At" annotation
    ax.text(0.95, -0.1, f'Last Updated At: {dt.datetime.now().time().strftime("%H:%M:%S")}', transform=ax.transAxes,
            fontsize=12, color='black', ha='right', va='top', backgroundcolor='lightgrey')

    # Re-add the annotation to make sure it appears on the updated plot
    ax.add_artist(annotation)

def main():
    global schwab_client
    global annotation
    global ax
    global fig
    global max_interest
    global graph_max, graph_min
    global _ticker
    global update_rate
    global expiry_date_string
    global to_date_obj, from_date_obj
    global yesterday_ohlc
    global adr5

    parser = argparse.ArgumentParser()
    parser.add_argument('--reauth', action='store_true', help="Force reauthorize?")
    parser.add_argument('--ticker', type=str, help="Stock ticker")
    parser.add_argument('--update_rate', type=int, help="Update rate")
    parser.add_argument('--expiry', type=str, help="Expiry date")
    args = parser.parse_args()

    if args.reauth:
        schwab_client = initialize_schwab_client(reauthorize=True)
    else:
        schwab_client = initialize_schwab_client(reauthorize=False)

    if args.ticker:
        _ticker = str(args.ticker).upper()

    if args.update_rate:
        update_rate = int(args.update_rate)

    if args.expiry:
        expiry_date_string = str(args.expiry)

    hist = get_ohlc_history(days=5)
    adr5 = calculate_adr(hist, 5)
    
    expiry_date_string = get_the_next_friday(dt.datetime.now().date())
    from_date_obj = dt.datetime.strptime(expiry_date_string, "%Y-%m-%d").date()
    to_date_obj = dt.datetime.strptime(expiry_date_string, "%Y-%m-%d").date()

    _, open_price, last_price, _, _, _, _ = update_data(from_date=from_date_obj, to_date=to_date_obj)    
    graph_min = last_price - span
    graph_max = last_price + span
    _, open_price, last_price, _, _, _, max_interest = update_data(from_date=from_date_obj, to_date=to_date_obj, lower_strike=graph_min, upper_strike=graph_max)
    
    if show_yesterday:
        yesterday_ohlc = get_yesterdays_ohlc()

    fig, ax = plt.subplots(figsize=(24, 8))

    # Set the background color for the entire figure
    fig.patch.set_facecolor('lightgrey')  # Set figure background color

    # Annotation for showing the strike price and open interest
    annotation = ax.annotate("", xy=(0,0), xytext=(20,20),
                            textcoords="offset points",
                            bbox=dict(boxstyle="round", fc="w"),
                            arrowprops=dict(arrowstyle="->"))
    annotation.set_visible(False)

    # Set the background color for the plot area (axes)
    ax.set_facecolor('#f5deb3')  # Set plot area background color

    # Adjust layout to reduce border space
    plt.subplots_adjust(left=0.05, right=0.95, top=0.95, bottom=0.15)
    update(0)

    ani = FuncAnimation(fig, update, interval=update_rate, cache_frame_data=False)  # Update every 5000 ms (5 second)

    fig.canvas.manager.set_window_title(_ticker.upper())

    # Connect the on_click event
    fig.canvas.mpl_connect("button_press_event", on_click)

    plt.show()

if __name__=='__main__':
    main()
