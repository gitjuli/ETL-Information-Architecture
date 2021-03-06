#get WDI API wrapper
import world_bank_data as wb

#pandas
import pandas as pd

# libraries to write to s3 bucket
import s3fs
import boto3
#read data from s3 and save to mysql RDS staging instance

import boto_config

def lambda_handler(event, context):
    #list of countries
    countries=wb.get_countries()
    countries.reset_index(inplace=True)

    #get regions
    regions=countries.region.unique()

    #create an index_region column with an autogenerated list and add to the dataframe
    index=list(range(1, len(regions)+1))
    regions=pd.DataFrame(regions,index).reset_index()
    regions.rename(columns={'index': "index_region", 0: "region"}, inplace=True)

    #get income levels
    countries=countries[countries.region != 'Aggregates']
    income_levels=countries.incomeLevel.unique()

    #create an index_income column with an autogenerated list and add to the dataframe
    index=list(range(1, len(income_levels)+1))
    income_levels=pd.DataFrame(income_levels,index).reset_index()
    income_levels.rename(columns={'index': "index_income", 0: "incomeLevel"}, inplace=True)

    # merging results to countries
    countries = pd.merge(countries, regions, how='inner', on=['region'])
    countries = pd.merge(countries, income_levels, how='inner', on=['incomeLevel'])

    # creating index_country to use later
    countries.reset_index(inplace=True)
    countries['index_country']=countries['index']+1
    countries.rename(columns={'id': "iso3Code"}, inplace=True)

    #get only countries, not regions
    countries=countries[countries.region != 'Aggregates']

    #delete unnecesary columns
    countries.drop(columns=['index'], inplace=True)

    #searching for child mortality indicator details (not actual values yet)
    child_mort_ind=wb.search_indicators('child mortality')

    #reseting index to allow 'id' to be a column in the dataframe
    child_mort_ind.reset_index(inplace=True)

    #getting under5 child mortality rate
    indicators=child_mort_ind[child_mort_ind.id == 'SH.DYN.MORT']

    #searching for Gross National Income (GNI) per capita indicator details (not actual values yet)
    gni_ind=wb.search_indicators('gni per capita').reset_index()
    gni_ind=gni_ind[(gni_ind.id == 'NY.GNP.PCAP.CD')]

    #append both indicators together
    indicators=indicators.append(gni_ind)

    #call WDI API to get SH.DYN.MORT => Mortality rate, under-5 (per 1,000 live births)	
    under5=wb.get_series('SH.DYN.MORT', simplify_index=True).to_frame().reset_index()
    under5.rename(columns={'Country': "name", 'Year': "year", "SH.DYN.MORT":"value"}, inplace=True)

    #merging with countries
    under5 = pd.merge(under5[under5['value'].notna()], countries, how='inner', on=['name'])
    under5.loc[:,'indicator']=1

    # creating index_under5_per_country to use later
    under5.reset_index(inplace=True)
    under5['index_under5_per_country']=under5['index']+1

    #call WDI API to get NY.GNP.PCAP.CD => Gross National Income per capita
    gni_per_capita=wb.get_series('NY.GNP.PCAP.CD', simplify_index=True).to_frame().reset_index()
    gni_per_capita.rename(columns={'Country': "name", 'Year': "year", "NY.GNP.PCAP.CD":"value"}, inplace=True)

    # merging with countries
    gni_per_capita = pd.merge(gni_per_capita[gni_per_capita['value'].notna()], countries, how='inner', on=['name'])
    gni_per_capita.loc[:,'indicator']=2

    save_to_s3('WDI', 'Region', regions)
    save_to_s3('WDI', 'Income_group', income_levels)
    save_to_s3('WDI', 'Country', countries[['name', 'iso2Code', 'iso3Code', 'index_region']])
    save_to_s3('WDI', 'Indicator', indicators)
    save_to_s3('WDI', 'Under5_per_country', under5[['value','year','indicator', 'index_country','name']])
    save_to_s3('WDI', 'Gni_per_country', gni_per_capita[['value','year','indicator', 'index_country']])

    # load income reference boundaries, which provides
    # the lower and upper bound (corresponding to GNI values)
    # for each year between 1987 and 2018 to determine the 
    # corresponding income level

    xls = pd.ExcelFile(r"s3://finalprojectgroup4/WDI/OGHIST.xls")
    df1 = pd.read_excel(xls, 'Country Analytical History')
    df_income=df1.iloc[4:9,1:]

    # use first row as column index
    df_income.columns = df_income.iloc[0]
    df_income=df_income.iloc[1:]

    # rearrange the dataframe from wide to long format
    # to have the columns, containing the years, as rows
    melted = pd.melt(df_income, ['Data for calendar year :'])
    melted.rename(columns={'Data for calendar year :': "income"}, inplace=True)
    melted.rename(columns={4: 'year'}, inplace=True)
    melted=melted[melted['income'] != 'Data for calendar year :']

    # use split to separate a string column 
    # like <= 480 into 480 as the upper bound
    # and 1,941-6,000 as 2 columns
    split=melted["value"].str.split('-|<=', n = 1, expand = True)
    split.columns = ['lower_bound', 'upper_bound']

    # replace the > remaining sign and the comma, leaving only integer values
    split.lower_bound=split.lower_bound.str.replace(r'>', '')
    split['lower_bound'] = split['lower_bound'].str.replace(',', '')#.astype(int)
    split['upper_bound'] = split['upper_bound'].str.replace(',', '')

    #fill empty upper bound with 1000000
    split.upper_bound.fillna(value=1000000, inplace=True)

    #replace empty lower bounds with zero
    split.loc[split.lower_bound == '', 'lower_bound']= 0

    # append the new columns to the long format data frame using the pd.concat() function
    melted = pd.concat([melted, split], axis=1)

    # delete the original 'value' column since it is no longer needed
    melted.drop(columns=['value'], inplace = True)

    #save data to s3
    save_to_s3('WDI', 'Income_boundaries', melted)

def save_to_s3(folder, filename, data):
    """Funtion that saves a dataframe as a csv file in a s3 bucket, receives 3 parameters:
    folder: corresponds to the folder in s3 where it will save the csv
    filename: the filename for the file
    data: the dataframe that will turn into a csv"""
    
    #prepare location
    location = 's3:/finalprojectgroup4/'+folder+'/'
    
    #prepare filname
    filenames3 = "%s%s.csv"%(location,filename)
    
    #encodes file as binary
    byte_encoded_df = data.to_csv(None).encode() 
    s3 = s3fs.S3FileSystem(anon=False, key=boto_config.key_id, secret=boto_config.secret_key)
    with s3.open(filenames3, 'wb') as file:
        
        #writes byte-encoded file to s3 location
        file.write(byte_encoded_df)

    #print success message
    print("Successfull uploaded file to location:"+str(filenames3))