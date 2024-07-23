import asyncio
import aiohttp
import json
import os
import re
import time
from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning, Comment
from urllib.parse import urlparse, urljoin
import warnings
from openai import OpenAI
from tenacity import retry, stop_after_attempt, wait_random_exponential
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Ignore XMLParsedAsHTMLWarning
warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

# Add OpenAI API key
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

@retry(wait=wait_random_exponential(min=1, max=60), stop=stop_after_attempt(6))
async def get_gpt4_response(prompt):
    try:
        response = await asyncio.to_thread(
            client.chat.completions.create,
            model="gpt-4o-mini",  
            messages=[
                {"role": "system", "content": "You are a helpful assistant that extracts and cleans product information from HTML content."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.7,
            max_tokens=4000  # Increased max tokens for longer responses
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        print(f"Error in OpenAI API call: {e}")
        raise

def preprocess_html(html):
    soup = BeautifulSoup(html, 'lxml')

    # Remove script and style elements
    for script in soup(["script", "style"]):
        script.decompose()

    # Remove comments
    for comment in soup.find_all(string=lambda string: isinstance(string, Comment)):
        comment.extract()

    # Instead of removing, let's just mark potential non-product areas
    for elem in soup(['header', 'footer', 'nav']):
        elem['data-section'] = 'non-product'

    for elem in soup(class_=re.compile(r'menu|sidebar|ad|comment|footer|header|navigation')):
        elem['data-section'] = 'non-product'

    for elem in soup(id=re.compile(r'menu|sidebar|ad|comment|footer|header|navigation')):
        elem['data-section'] = 'non-product'

    # Convert to string, maintaining the structure
    html_string = str(soup)

    # Add a note for GPT about the marked sections
    html_string = "<!-- Sections marked with data-section='non-product' are likely not part of the main product information -->\n" + html_string

    return html_string

async def process_product_urls(session, product_urls, folder_name):
    for i, url in enumerate(product_urls):
        if i > 0 and i % 15 == 0:
            await asyncio.sleep(60)  # Wait for 60 seconds after every 15 requests

        try:
            product_name = url.split('/')[-1]
            file_name = os.path.join(folder_name, f"{product_name}.json")
            
            if os.path.exists(file_name):
                print(f"File {file_name} already exists. Skipping.")
                continue

            async with session.get(url, headers={"User-Agent": "Mozilla/5.0"}) as response:
                if response.status == 200:
                    html = await response.text()
                    
                    # Preprocess HTML
                    preprocessed_html = preprocess_html(html)
                    
                    # Prepare prompt for GPT-4
                    prompt = f"""This is the HTML content of an e-commerce product page. Some sections are marked with data-section='non-product' to indicate they might not be part of the main product information. Please extract and present the product information in JSON format, focusing on the unmarked sections but also considering marked sections if they contain relevant product details. Include core product attributes like product name, description, price, and any other available attributes. Preserve the original wording and details as provided by the brand. Make it detailed and comprehensive. Respond with just your polished cleaned JSON version. Here is the HTML content:

{preprocessed_html}"""

                    try:
                        # Get response from GPT-4
                        cleaned_text = await get_gpt4_response(prompt)
                    except Exception as e:
                        if "context_length_exceeded" in str(e):
                            print(f"Context length exceeded for {url}. Retrying with reduced content...")
                            # Remove header and footer content
                            soup = BeautifulSoup(html, 'lxml')
                            for elem in soup(['header', 'footer', 'nav']):
                                elem.decompose()
                            reduced_html = str(soup)
                            reduced_prompt = f"""This is the reduced HTML content of an e-commerce product page with header and footer removed. Please extract and present the product information in JSON format. Include core product attributes like product name, description, price, and any other available attributes. Preserve the original wording and details as provided by the brand. Make it detailed and comprehensive. Respond with just your polished cleaned JSON version. Here is the reduced HTML content:

{reduced_html}"""
                            cleaned_text = await get_gpt4_response(reduced_prompt)
                        else:
                            raise e

                    # Clean up the JSON response
                    cleaned_json = cleaned_text.strip()
                    if cleaned_json.startswith('```json'):
                        cleaned_json = cleaned_json[7:]
                    if cleaned_json.endswith('```'):
                        cleaned_json = cleaned_json[:-3]
                    
                    # Parse and re-serialize the JSON to ensure proper formatting
                    try:
                        json_data = json.loads(cleaned_json)
                        json_data['url'] = url  # Add the URL to the JSON data
                        
                        with open(file_name, 'w', encoding='utf-8') as f:
                            json.dump(json_data, f, ensure_ascii=False, indent=2)
                        print(f"Saved cleaned content for {file_name}")
                    except json.JSONDecodeError as e:
                        print(f"Error parsing JSON for {url}: {e}")
                        # Optionally, save the raw text if JSON parsing fails
                        with open(f"{file_name}.txt", 'w', encoding='utf-8') as f:
                            f.write(f"URL: {url}\n\n")
                            f.write(cleaned_text)
                        print(f"Saved raw text for {file_name}.txt due to JSON parsing error")
                else:
                    print(f"Failed to retrieve data for {url}. Status code: {response.status}")
        except Exception as e:
            print(f"An error occurred for {url}: {e}")


async def get_internal_links(session, base_url, url, visited_urls, file, max_retries=3):
    if url in visited_urls:
        return

    visited_urls.add(url)
    print(f"Crawling: {url}")
    file.write(url + '\n')
    file.flush()

    for attempt in range(max_retries):
        try:
            async with session.get(url, timeout=30) as response:
                if response.status == 200:
                    html = await response.text()
                    soup = BeautifulSoup(html, 'lxml')  # Using lxml parser
                    sub_links = [urljoin(base_url, a['href']) for a in soup.find_all('a', href=True)]
                    tasks = []
                    for link in sub_links:
                        parsed_link = urlparse(link)
                        if parsed_link.netloc == urlparse(base_url).netloc and link not in visited_urls:
                            task = asyncio.ensure_future(get_internal_links(session, base_url, link, visited_urls, file))
                            tasks.append(task)
                    await asyncio.gather(*tasks)
                else:
                    print(f"Failed to retrieve data from {url}. Status code: {response.status}")
                break
        except asyncio.TimeoutError:
            if attempt < max_retries - 1:
                print(f"Timeout occurred for {url}. Retrying... (Attempt {attempt + 1}/{max_retries})")
                await asyncio.sleep(1)
            else:
                print(f"Max retries reached for {url}. Moving on...")
        except Exception as e:
            print(f"An error occurred for {url}: {e}")
            break

async def main():
    homepage_url = input("Enter the homepage URL you'd like to crawl: ")

    if not homepage_url.startswith(('http://', 'https://')):
        homepage_url = 'http://' + homepage_url

    print(f"Starting to crawl from: {homepage_url}")

    folder_name = urlparse(homepage_url).netloc
    os.makedirs(folder_name, exist_ok=True)
    file_name = os.path.join(folder_name, "full_sitemap.txt")
    visited_urls = set()

    async with aiohttp.ClientSession() as session:
        with open(file_name, 'w', encoding='utf-8') as file:
            await get_internal_links(session, homepage_url, homepage_url, visited_urls, file)

    print(f"Crawling completed. Sorting URLs...")
    
    # Read the file, normalize and deduplicate URLs, then sort them
    with open(file_name, 'r', encoding='utf-8') as file:
        urls = file.readlines()
    
    # Normalize and deduplicate URLs
    normalized_urls = set()
    for url in urls:
        # Remove whitespace, convert to lowercase, and remove trailing slashes
        normalized_url = url.strip().lower().rstrip('/')
        # Remove query parameters
        normalized_url = normalized_url.split('?')[0]
        normalized_urls.add(normalized_url)
    
    sorted_urls = sorted(normalized_urls)
    
    with open(file_name, 'w', encoding='utf-8') as file:
        for url in sorted_urls:
            file.write(url + '\n')

    print(f"All internal links have been saved and sorted in {file_name}")
    print(f"Total unique URLs found: {len(sorted_urls)}")

    # Process product URLs
    product_urls = [url for url in sorted_urls if '/products/' in url]
    print(f"Found {len(product_urls)} unique product URLs. Processing with Jina AI...")

    async with aiohttp.ClientSession() as session:
        await process_product_urls(session, product_urls, folder_name)

    print("Finished processing product URLs with Jina AI.")

if __name__ == "__main__":
    asyncio.run(main())
