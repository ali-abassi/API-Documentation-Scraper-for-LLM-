import asyncio
import aiohttp
import requests
import re
import os
import time
from urllib.parse import urlparse
from openai import OpenAI
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

def get_user_url():
    url = input("Please enter a URL for the documentation: ").strip()
    if not url.startswith(('http://', 'https://')):
        url = 'https://' + url
    return url

def is_valid_url(url):
    try:
        result = urlparse(url)
        return all([result.scheme, result.netloc])
    except ValueError:
        return False

async def scrape_url_async(session, url, max_retries=3):
    jina_api_url = f"https://r.jina.ai/{url}"
    headers = {
        "Authorization": f"Bearer {os.getenv('JINA_API_KEY')}",
        "X-Timeout": "10"
    }
    
    for attempt in range(max_retries):
        try:
            async with session.get(jina_api_url, headers=headers) as response:
                response.raise_for_status()
                return await response.text()
        except aiohttp.ClientError as e:
            if attempt == max_retries - 1:
                print(f"Error scraping URL after {max_retries} attempts: {e}")
                return None
            await asyncio.sleep(2 ** attempt)  # Exponential backoff

async def scrape_urls_concurrently(urls):
    async with aiohttp.ClientSession() as session:
        tasks = [scrape_url_async(session, url) for url in urls]
        return await asyncio.gather(*tasks)

def extract_urls(content):
    url_pattern = r'https?://[^\s)\]"]+'
    urls = re.findall(url_pattern, content)
    unique_urls = []
    seen = set()
    for url in urls:
        if url not in seen and is_valid_url(url):
            seen.add(url)
            unique_urls.append(url)
    return unique_urls

def get_filename_from_url(url):
    parsed_url = urlparse(url)
    domain_parts = parsed_url.netloc.split('.')
    if len(domain_parts) > 2 and domain_parts[0] != 'www':
        domain = f"{domain_parts[0]}_{domain_parts[1]}"
    elif domain_parts[0] == 'www':
        domain = domain_parts[1]
    else:
        domain = domain_parts[0]
    return f"{domain}_docs.txt"

def create_content_section(content, url, index):
    separator = "=" * 80
    return f"\n{separator}\nSection {index}: Content from {url}\n{separator}\n\n{content}\n\n"

def write_content_to_file(filename, sections):
    with open(filename, 'w', encoding='utf-8') as file:
        file.write("".join(sections))

def filter_urls(urls, base_url):
    client = OpenAI(api_key=os.getenv('OPENAI_API_KEY'))
    urls_text = "\n".join(urls)
    base_domain = urlparse(base_url).netloc
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": "You are a URL curator tasked with filtering out obviously unrelated content from a list of URLs for a software tool or API."},
            {"role": "user", "content": f"""We need to extract documentation for a tool with the base domain {base_domain}. Here's a list of URLs we've found:

{urls_text}

Please filter this list based on the following criteria:
1. Keep URLs that appear to be related to documentation, guides, tutorials, or API references.
2. Include relevant subdomains like 'docs.{base_domain}', 'api.{base_domain}', or 'developer.{base_domain}'.
3. Remove URLs for obviously unrelated content such as community forums, status pages, blog posts, or contact pages.

Respond with a list of filtered URLs, one per line, without any additional text or formatting. Ensure all URLs are valid."""}
        ]
    )
    filtered_urls = [url.strip() for url in response.choices[0].message.content.strip().split('\n') if is_valid_url(url.strip())]
    return filtered_urls

async def main_async():
    user_url = get_user_url()
    print(f"You entered: {user_url}")

    async with aiohttp.ClientSession() as session:
        initial_content = await scrape_url_async(session, user_url)
        if initial_content:
            urls = extract_urls(initial_content)
            print(f"Found {len(urls)} unique URLs.")
            
            filtered_urls = filter_urls(urls, user_url)
            print(f"Filtered down to {len(filtered_urls)} relevant URLs.")
            
            filename = get_filename_from_url(user_url)
            contents = await scrape_urls_concurrently(filtered_urls)
            
            sections = []
            for i, (url, content) in enumerate(zip(filtered_urls, contents), 1):
                if content:
                    sections.append(create_content_section(content, url, i))
                    print(f"URL #{i} Scraped ✅ - {url}")
                else:
                    print(f"URL #{i} Found an Error and Skipped ❌ - {url}")
            
            write_content_to_file(filename, sections)
            print(f"📃 Content saved to {filename}. 📃")
        else:
            print("❌ Failed to scrape the initial URL. ❌")

if __name__ == "__main__":
    asyncio.run(main_async())
