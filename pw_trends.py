import asyncio
import time
import requests
import re
import json
import base64
import os
import random
from datetime import datetime
from urllib.parse import urljoin
from google import genai
from dotenv import load_dotenv

from playwright.async_api import async_playwright

load_dotenv()


GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_API_GPT = os.getenv("GEMINI_API_GPT")
WP_URL = os.getenv("WP_BLOG_FOFOCANDO_URL")
WP_USER = os.getenv("WP_BLOG_FOFOCANDO_USER")
WP_PASS = os.getenv("WP_BLOG_FOFOCANDO_PASS")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
TRENDS_URL = os.getenv("TRENDS_URL_ENTERTEINMENT")

async def main():

    async def get_image_element(page):
        selectors = ["figure img", "picture img", "section img", "article img", "div img"]

        for selector in selectors:
            img = await page.query_selector(selector)
            if img:
                return img

        print("Nenhuma imagem encontrada com os seletores fornecidos.")
        return None
    
    async def dowload_cover_image(page, href):

        await page.goto(href, wait_until="domcontentloaded", timeout=120000)

        title_locator = page.locator('h1').first
        title = await title_locator.text_content()

        safe_title = re.sub(r'[^a-zA-Z0-9_\-]', '_', title)

        img_locator = await get_image_element(page)

        srcset = await img_locator.get_attribute("srcset")
        if srcset:
            largest = srcset.split(",")[-1].split()[0].strip()
            img_src = urljoin(href, largest)
        else:
            img_src = await img_locator.get_attribute('src')
            img_src = urljoin(href, img_src.replace('x240', 'x720'))  # Garante que o link da imagem seja absoluto

        if img_src:
            # Faz o download da imagem
            response = requests.get(img_src)
            img_path = f'covers/{safe_title}.jpg'

            if response.status_code == 200:
                with open(f'covers/{safe_title}.jpg', 'wb') as f:
                    f.write(response.content)
            else:
                print(f'Falha ao baixar a imagem: Status {response.status_code}')
        else:
            print('Nenhuma imagem encontrada.')

        return img_path, safe_title
    
    async def generate_content_ai(href, href2, href3, links_wordpress):
        
        prompt = f"""
        Crie um post para blog possuindo um texto já em html (apenas o corpo do texto para inserir no editor do Wordpress) com retorno em formato json 
        (apenas os campos 'title', 'slug', 'meta_description', 'keyword' e 'body'), com no minimo 600 palavras e links externos (deve possuir links de saída!). Os links internos devem vir dos seguintes links: {links_wordpress}.
        O campo 'title' deve conter no máximo 57 caracteres.
        A keyword deve ser a mais importante do texto, e deve ser usada no título, no slug, na meta description e no corpo do texto.
        A keyword, ou sinônimos dela, devem ser exibidos em até 70% dos subtítulos H2 e H3 (não em todos os subtítulos!).
        O campo 'meta description' deve conter no mínimo 120 caracteres e no máximo 146 caracteres.
        Todo o texto deve respeitar as regras de SEO, se baseando nas seguintes noticias dos links: {href} {href2} {href3}. 
        O conteúdo html no campo 'body' deve ser inserido dentro de uma tag <article>. E dentro da tag <article> não deve conter a tag <h1> e nem deve haver sugestões para o autor do post no meio do texto. 
        O primeiro parágrafo não deve conter a tag <h2> e deve ser uma introdução ao assunto do post, sem repetir o título do post.
        A idéia é que o texto já esteja pronto para ser publicado no Wordpress.
        """

        client = genai.Client(api_key=GEMINI_API_KEY)

        response = client.models.generate_content(
            model=GEMINI_API_GPT, contents=prompt
        )

        resposta_limpa = response.text.strip('`').replace('json\n', '', 1).strip()

        return json.loads(resposta_limpa)

    async def auth_wordpress():
        username = WP_USER
        application_password = WP_PASS

        # Codifica a autenticação em Base64
        credentials = f'{username}:{application_password}'
        token = base64.b64encode(credentials.encode())
        headers_auth = {
            'Authorization': f'Basic {token.decode("utf-8")}'
        }

        return headers_auth
    
    async def upload_image_to_wordpress(headers_auth, site_url, image_path, match):
        # 1️⃣ Upload da imagem para /media
        with open(image_path, 'rb') as img:
            image_data = img.read()

        media_headers = {
            **headers_auth,
            'Content-Disposition': 'attachment; filename="imagem.jpg"',
            'Content-Type': 'image/jpeg'
        }

        post_data = {
                'title': match['title'],
                'alt_text': match['title'],
                'caption': match['title'],
                'description': match['meta_description']
                }

        media_response = requests.post(
            f'{site_url}/wp-json/wp/v2/media',
            headers=media_headers,
            data=image_data
        )

        if media_response.status_code != 201:
            print('Erro ao fazer upload da imagem:', media_response.text)

        media_id = media_response.json()['id']

        media_headers = {
            **headers_auth,
            'Content-Type': 'application/json'
        }
        meta_response = requests.put(
            f"{site_url}/wp-json/wp/v2/media/{media_id}",
            headers=media_headers,
            json=post_data
        )

        if meta_response.status_code != 200:
            print("Erro ao atualizar metadados:", meta_response.text)

        return media_id

    async def create_post_wordpress(headers_auth, site_url, media_id, match, link):
        # 2️⃣ Criação do post com imagem destacada
        post_data = {
            'title': match['title'],
            'content': match['body'],
            'status': 'publish',
            'featured_media': media_id,
            'slug': match['slug'],
            'yoast_description': match['meta_description'],
            'yoast_keyword': match['keyword'],
            'categories': [
                9 if re.search('Vale Tudo', match['title']) is not None else 6,  # 6 = Noticias, 9 = Novelas
            ],
            'author': random.choice([2,3,4]),
        }

        post_headers = {
            **headers_auth,
            'Content-Type': 'application/json'
        }

        post_response = requests.post(
            f'{site_url}/wp-json/wp/v2/posts',
            headers=post_headers,
            json=post_data
        )

        if post_response.status_code == 201:
            post_link = post_response.json()['link']
            log_message = f'✅ Post criado com sucesso! Em {datetime.now().strftime('%d/%m/%Y %H:%M:%S')} => Link {link}: {post_link}'
            print(log_message)
        else:
            log_message = f'❌ Erro ao criar o post: {post_response.text} - Em {datetime.now().strftime('%d/%m/%Y %H:%M:%S')} => Link {link}'
            print(log_message)
        
        return log_message    

    async def search_trends(link, ref = 1):
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context()

            # Create a new page in the provided browser context
            page = await context.new_page()
    
            await page.goto(TRENDS_URL)
            await page.click(f'//*[@id="trend-table"]/div[1]/table/tbody[2]/tr[{link}]')
            
            href = await page.locator('.jDtQ5 a').first.get_attribute('href')
            href2 = await page.locator('.jDtQ5 a').nth(1).get_attribute('href')
            href3 = await page.locator('.jDtQ5 a').nth(2).get_attribute('href')

            await page.set_extra_http_headers({
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            })

            link_image = href if ref == 1 else href2 if ref == 2 else href3

            info_image_path = await dowload_cover_image(page, link_image)
            image_path, safe_title = info_image_path

            await browser.close()

            return href, href2, href3, image_path, safe_title

    def remove_image(image_path):
        img_local = os.path.join("covers", f'{image_path}.jpg')
        if os.path.exists(img_local):
            os.remove(img_local)

    async def recover_links_wordpress():
        headers_auth = await auth_wordpress()

        response = requests.get(f'{WP_URL}/wp-json/wp/v2/posts', headers=headers_auth)

        if response.status_code == 200:
            posts = response.json()
            posts_links = [post['link'] for post in posts]
        else:
            print('Erro ao recuperar links do WordPress:', response.text)
            posts_links = []        
        
        response = requests.get(f'{WP_URL}/wp-json/wp/v2/categories', headers=headers_auth)

        if response.status_code == 200:
            posts = response.json()
            categories_links = [post['link'] for post in posts]
            
        else:
            print('Erro ao recuperar links do WordPress:', response.text)
            categories_links = []     

        return posts_links + categories_links       

    async def send_telegram_message(log_message):
        
        url = f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage'
        payload = {
            'chat_id': TELEGRAM_CHAT_ID,
            'text': log_message
        }
        
        response = requests.post(url, data=payload)
        
        if response.status_code != 200:
            print(f'Falha ao enviar mensagem: {response.text}')

    async def run_task(link):

        links_wordpress = await recover_links_wordpress()

        href, href2, href3, image_path, safe_title = await search_trends(link)

        match = await generate_content_ai(href, href2, href3, links_wordpress)

        headers_auth = await auth_wordpress()

        # 1️⃣ Upload da imagem para /media
        media_id = await upload_image_to_wordpress(headers_auth, WP_URL, image_path, match)

        # 2️⃣ Criação do post com imagem destacada
        log_message = await create_post_wordpress(headers_auth, WP_URL, media_id, match, link)
        remove_image(safe_title)

        await send_telegram_message(log_message)
    
    link = 18
    ref = 1
    while True:
        
        tentativas = 0
        while tentativas < 5:
            try:
                await run_task(link)
                if link == 25:
                    link = 1
                    ref = 1
                else:
                    link += 1
                break  # Se a tarefa for executada com sucesso, sai do loop de tentativas
            except Exception as e:
                print(f'❌ Em {datetime.now()} - Ocorreu um erro: {e}. Tentativa {tentativas + 1} de 5. Link: {link}, Ref: {ref}')
                tentativas += 1
                if tentativas == 5 and link < 25 and ref < 3:
                    tentativas = 0
                    ref += 1
                
                if tentativas == 5 and link < 25 and ref == 3:
                    tentativas = 0
                    ref = 1    
                    link += 1
        # await asyncio.sleep(14400)  # 14400 segundos = 4 horas
        # await asyncio.sleep(7200)  # 7200 segundos = 2 horas
        await asyncio.sleep(30*60)  # 1800 segundos = 30 min

if __name__ == "__main__":
    asyncio.run(main())
